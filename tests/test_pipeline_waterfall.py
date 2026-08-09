"""Every step of the waterfall is promoted by a driver the product ships.

This is the test the project lacked. Under the nine stages this replaced, one
stage was promoted by nothing at all: the only code that ever advanced it lived
in this file, so a real run stopped one refusal short of the review page while
the suite stayed green. The shape here answers that directly. Nothing below
calls :mod:`glite_english_audit.pipeline.record_stage`; the test runs the five
drivers and then reads the run manifest. If a step's promotion disappears from
the product, nothing here puts it back.

Steps c, d and e are one agent per session file in production. A fixed rule
plays that part so the run is reproducible; everything else is the real thing,
over the committed synthetic fixtures, with no model and no network anywhere.

Three things the nine stages had are not steps any more, and are reached here
through the run-level paths that own them: the source inventory and the snapshot
manifests sit beside the run manifest, and the review — which produces one
artifact for the whole run and then waits on a person — lives in ``submission/``.
"""

import importlib
import itertools
import json
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    OsEnvironment,
    Stability,
    StageStatus,
    StepId,
)
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    MistakeRecord,
    NormalizedUtterance,
    ReviewedSubmissionArtifact,
    SourceInstanceRecord,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery import registry
from glite_english_audit.discovery.base import (
    DiscoveryContext,
    DiscoveryOutcome,
    SnapshotCapture,
)
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.paths import inventory_path, snapshot_dir, step_dir, submission_dir
from glite_english_audit.pipeline import (
    authorship,
    build_review,
    collect,
    deduplicate,
    mistakes,
    start_run,
    verify,
)
from glite_english_audit.pipeline.mistakes import read_records
from glite_english_audit.sessions import read_index, read_session, session_files
from glite_english_audit.state.run_store import load_manifest
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)
from glite_english_audit.verification.step_layout import (
    compare_file_sets,
    compare_line_counts,
    index_carried_forward,
)

_FIXTURE_HOME = (
    Path(__file__).resolve().parent.parent / "fixtures" / "claude_code" / "success" / "home"
)

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

# The module an agent runs for each step, straight out of the skill it follows.
# A step missing from this table has no driver, which is the failure this whole
# file exists to make impossible.
_DRIVERS: dict[StepId, str] = {
    StepId.A_COLLECTED: "glite_english_audit.pipeline.collect",
    StepId.B_DEDUPLICATED: "glite_english_audit.pipeline.deduplicate",
    StepId.C_AUTHORED: "glite_english_audit.pipeline.authorship",
    StepId.D_MISTAKES: "glite_english_audit.pipeline.mistakes",
    StepId.E_VERIFIED: "glite_english_audit.pipeline.verify",
}

# One fixed judgment per construction the synthetic fixture plants, standing in
# for the model: the phrase to look for, then the three shareable sentences a
# step-d agent writes about it. Every example is invented here rather than
# quoted, which is what step d owes and what the privacy scanner re-checks.
_MARKED: tuple[tuple[str, str, str, str], ...] = (
    (
        "I very like this plan",
        "Used 'very' to modify a verb directly.",
        "'Very' cannot modify a verb; use 'really' instead.",
        "I really like this plan.",
    ),
    (
        "Today I written",
        "Used the past participle where the simple past belongs.",
        "The simple past of 'write' is 'wrote', not 'written'.",
        "Today I wrote a short note.",
    ),
    (
        "I am agree",
        "Put a form of 'be' in front of a verb.",
        "'Agree' is a verb, so it takes no form of 'be'.",
        "I agree with the second option.",
    ),
)

# The fixture message the authorship stand-in treats as someone else's text.
_PASTED = "deploy script"

# What a step-e agent withholds here, so the withheld count has something real
# to describe. It is the only record in its session, which also exercises the
# rule that a session sharing nothing is an empty file rather than a missing one.
_WITHHELD = _MARKED[0][1]

_INVENTED_SPAN = "a sentence the learner never wrote"


@dataclass(frozen=True)
class Workspace:
    """A checkout with the run store inside it, as production has it."""

    repo: Path
    inventory_dir: Path
    runs_root: Path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    inventory_dir = ensure_private_dir(tmp_path / "inventory")
    outcome = claude_code_adapter().discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS, home=_FIXTURE_HOME, now=_NOW, environ={}
        )
    )
    assert outcome.records, "discovery must find the fixture instances"
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(
            records=outcome.records,
            instance_paths={key: str(path) for key, path in outcome.instance_paths.items()},
            created_at=_NOW,
        ),
    )
    return Workspace(repo=repo, inventory_dir=inventory_dir, runs_root=repo / "runtime" / "runs")


def _start(workspace: Workspace) -> str:
    """Create the run and freeze its selection. Returns the run ID."""
    manifest = start_run.start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=workspace.runs_root,
        inventory_dir=workspace.inventory_dir,
        local_scan_consent=True,
        provider_transfer_consent=True,
        now=_NOW,
    )
    assert manifest.selection is not None
    assert manifest.selection.selected_instance_keys
    return manifest.run_id


def _count(result: Mapping[str, object], key: str) -> int:
    value = result[key]
    assert isinstance(value, int), f"{key} is {value!r}"
    return value


# -- the agents the drivers hand work to -----------------------------------


def _authored(text: str) -> str:
    """The spans of one message the learner actually wrote.

    Fixed rather than clever: a message about the deploy script is treated as
    pasted and comes back with empty text, and everything else is the learner's
    own. Step c requires the item to survive with its text emptied, because a
    vanished item and an unauthored one mean different things downstream.
    """
    return "" if _PASTED in text else text


def _judge_authorship(workspace: Workspace, run_id: str, *, spoil: str | None = None) -> None:
    """Run step c's ``--prepare`` and write the file each agent owes back.

    ``spoil`` names a session file whose judgment claims a span the step-b text
    does not contain — the failure the deterministic span scan must quarantine.
    """
    prepared = authorship.prepare(run_id, runs_root=workspace.runs_root)
    for session in prepared.sessions:
        # A file already on disk was judged before the interruption, so the
        # agents are not asked to pay for it twice.
        if session.already_written:
            continue
        judged = [
            utterance.model_copy(update={"text": _authored(utterance.text)})
            for utterance in read_session(Path(session.input_path))
        ]
        if spoil == session.file_name:
            judged[0] = judged[0].model_copy(update={"text": _INVENTED_SPAN})
        write_jsonl_models(Path(session.output_path), judged)


def _records_for(session: list[NormalizedUtterance]) -> list[MistakeRecord]:
    """One shareable record per marked construction in one session file."""
    records: list[MistakeRecord] = []
    for utterance in session:
        for phrase, what, rule, example in _MARKED:
            start = utterance.text.find(phrase)
            if start < 0:
                continue
            records.append(
                MistakeRecord(
                    utterance_id=utterance.utterance_id,
                    evidence_span=EvidenceSpan(start=start, end=start + len(phrase)),
                    mistake=what,
                    rule=rule,
                    example=example,
                    example_type=ExampleType.SYNTHETIC,
                    source_type=utterance.source_adapter,
                    modality=utterance.modality,
                )
            )
    return records


def _write_mistakes(workspace: Workspace, run_id: str) -> None:
    """Run step d's ``--prepare`` and write one mistake file per session."""
    for assignment in mistakes.prepare_mistakes(run_id, runs_root=workspace.runs_root):
        write_jsonl_models(
            Path(assignment.write), _records_for(read_session(Path(assignment.read)))
        )


def _confirm_mistakes(workspace: Workspace, run_id: str) -> None:
    """Write step e's files: step d's records again, minus the withheld one.

    Step e may only drop, never add or alter, so the stand-in copies the file
    line for line and removes one record. Its driver is what enforces that.
    """
    source = step_dir(run_id, StepId.D_MISTAKES, root=workspace.runs_root)
    target = step_dir(run_id, StepId.E_VERIFIED, root=workspace.runs_root)
    for path in session_files(source):
        produced, parse_diagnostics = read_records(path)
        assert parse_diagnostics == []
        write_jsonl_models(
            target / path.name, [record for record in produced if record.mistake != _WITHHELD]
        )


def _run_step(workspace: Workspace, run_id: str, step: StepId, *, spoil: str | None = None) -> None:
    """Run one step exactly as its skill does: prepare, agents, apply."""
    if step is StepId.A_COLLECTED:
        collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    elif step is StepId.B_DEDUPLICATED:
        deduplicate.deduplicate(run_id, runs_root=workspace.runs_root)
    elif step is StepId.C_AUTHORED:
        _judge_authorship(workspace, run_id, spoil=spoil)
        authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
    elif step is StepId.D_MISTAKES:
        _write_mistakes(workspace, run_id)
        mistakes.apply_mistakes(run_id, runs_root=workspace.runs_root)
    else:
        _confirm_mistakes(workspace, run_id)
        verify.apply_verification(run_id, runs_root=workspace.runs_root)


def _advance_through(
    workspace: Workspace, run_id: str, last: StepId, *, spoil: str | None = None
) -> None:
    """Run every step up to and including ``last`` that is not promoted yet."""
    manifest = load_manifest(run_id, root=workspace.runs_root)
    for step in StepId:
        if step > last:
            break
        if manifest.stages[step].status is StageStatus.PROMOTED:
            continue
        _run_step(workspace, run_id, step, spoil=spoil)


# -- every step has a driver, and the driver is what promotes it -----------


def test_the_step_table_names_every_step_exactly_once() -> None:
    # The table below is what the tests walk, so a step missing from it would
    # be a step nothing in this file ever asks about.
    assert sorted(_DRIVERS) == list(StepId)


@pytest.mark.parametrize("step", list(StepId), ids=lambda step: step.name.lower())
def test_each_step_is_promoted_by_a_driver_that_exists(workspace: Workspace, step: StepId) -> None:
    """A step nothing promotes is a step no run can get past.

    Two claims, and both are needed. The driver is a real module with a command
    line, so an agent can run it; and running the waterfall through it leaves
    that step promoted in the manifest without any help from this file. The
    stage that had neither passed a test that only ever asserted the first.
    """
    driver = importlib.import_module(_DRIVERS[step])
    assert callable(driver.main), f"{_DRIVERS[step]} has no command line"

    run_id = _start(workspace)
    _advance_through(workspace, run_id, step)

    manifest = load_manifest(run_id, root=workspace.runs_root)
    assert manifest.stages[step].status is StageStatus.PROMOTED
    # Nothing ran ahead of itself: every later step is still untouched, which is
    # what makes the promotion above attributable to this step's own driver.
    assert [
        later
        for later in StepId
        if later > step
        if manifest.stages[later].status is not StageStatus.PENDING
    ] == []


@pytest.mark.parametrize(
    "last",
    [None, *list(StepId)[:-1]],
    ids=["nothing", *[step.name.lower() for step in list(StepId)[:-1]]],
)
def test_the_review_refuses_to_publish_until_every_step_is_promoted(
    workspace: Workspace, last: StepId | None
) -> None:
    """The counts are the honesty guarantee, so a partial run may not produce them.

    Computed over part of a run they are still arithmetically consistent and
    still wrong, and nothing downstream can tell the difference — the reader
    sees a rate over a corpus that is not the one the person selected.
    """
    run_id = _start(workspace)
    if last is not None:
        _advance_through(workspace, run_id, last)

    with pytest.raises(ValueError, match="not promoted"):
        build_review.build_review(run_id, runs_root=workspace.runs_root)

    assert not (
        submission_dir(run_id, root=workspace.runs_root) / "reviewed-submission.json"
    ).exists()


# -- the whole waterfall ----------------------------------------------------


def test_the_waterfall_runs_step_by_step_and_reaches_a_publishable_review(
    workspace: Workspace,
) -> None:
    run_id = _start(workspace)
    runs_root = workspace.runs_root

    # The run's own copy of the inventory is one file at the run root beside the
    # manifest. It describes the machine rather than any one session, so it has
    # no per-session file to sit next to and is not a step.
    assert inventory_path(run_id, root=runs_root).is_file()

    # Step a: snapshot and extract. One session is one file from here on, named
    # by an opaque sequence number, with the index the only place a file name is
    # tied to a session.
    collected = collect.collect(run_id, runs_root=runs_root, repo=workspace.repo)
    assert collected["excluded_instances"] == []
    step_a = step_dir(run_id, StepId.A_COLLECTED, root=runs_root)
    names = [path.name for path in session_files(step_a)]
    assert len(names) == _count(collected, "sessions") > 1
    assert set(read_index(step_a)) == set(names)

    # The snapshots are verbatim copies of the user's own application data, so
    # they must be gone the moment extraction is durable.
    snapshots = snapshot_dir(run_id, repo=workspace.repo)
    leftover = [path for path in snapshots.rglob("*") if path.is_file()]
    assert leftover == [], "snapshots must be removed once extraction is durable"

    # Step b: comparison rather than judgment, so it runs before any model spends
    # tokens on text that is about to be discarded.
    deduplicated = deduplicate.deduplicate(run_id, runs_root=runs_root)
    step_b = step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root)
    assert _count(deduplicated, "messages_in") == _count(collected, "candidate_utterances")
    # Step b's files are no longer a faithful record of the sessions they name,
    # so what it dropped is written down rather than left to be inferred.
    assert (step_b / "removed.json").is_file()

    # Step c: one agent per session file, then the deterministic span check.
    _judge_authorship(workspace, run_id)
    applied = authorship.apply_authorship(run_id, runs_root=runs_root)
    assert applied.diagnostics == []
    assert applied.sessions_quarantined == 0
    # The agents dropped material the pre-filter had to keep, so the analyzed
    # denominator is strictly smaller than what they were offered.
    assert 0 < applied.words_after < applied.words_before

    # Step d: the records, already shareable when written.
    _write_mistakes(workspace, run_id)
    produced = mistakes.apply_mistakes(run_id, runs_root=runs_root)
    assert produced.passed
    assert produced.records == len(_MARKED)

    # Step e: a second reader that may drop and may do nothing else.
    _confirm_mistakes(workspace, run_id)
    confirmed = verify.apply_verification(run_id, runs_root=runs_root)
    assert confirmed.passed
    assert (confirmed.records_in, confirmed.records_dropped) == (len(_MARKED), 1)

    # The review is not a step: it produces one artifact for the whole run and
    # then waits on a person.
    reviewed = build_review.build_review(run_id, runs_root=runs_root)
    counts = reviewed.counts
    assert len(reviewed.records) == counts.shared_mistakes == len(_MARKED) - 1
    assert all(record.included for record in reviewed.records)
    withheld = (
        counts.withheld_by_user + counts.withheld_for_privacy + sum(counts.other_withheld.values())
    )
    assert counts.shared_mistakes + withheld == counts.verified_total_mistakes == len(_MARKED)
    assert counts.withheld_for_privacy == 1
    assert counts.analyzed_english_words > 0

    # The artifact on disk is what the review server would load, and it lives in
    # the run's submission directory rather than in a step directory.
    stored = read_model(
        submission_dir(run_id, root=runs_root) / "reviewed-submission.json",
        ReviewedSubmissionArtifact,
    )
    assert stored.counts == counts

    # And it materializes into a package that passes the full gate.
    package = materialize_package(stored)
    assert verify_submission_package(package) == []
    assert verify_package_against_review(package, stored) == []
    assert len(package.records) == counts.shared_mistakes


def test_every_step_holds_the_same_session_files(workspace: Workspace) -> None:
    """The invariant the five-step shape exists to provide, over a whole run.

    No single step can check it about itself, and the failure is quiet by
    nature: a step that skips a file it could not process leaves a run that
    looks finished and reports counts over less text than the person selected.
    Nothing downstream notices, because every later step reads only what it was
    handed.
    """
    run_id = _start(workspace)
    _advance_through(workspace, run_id, StepId.E_VERIFIED)

    directories = [step_dir(run_id, step, root=workspace.runs_root) for step in StepId]
    for previous, current in itertools.pairwise(directories):
        assert compare_file_sets(previous, current) == [], f"{previous.name} -> {current.name}"
        assert index_carried_forward(previous, current) == []
    held = [{path.name for path in session_files(directory)} for directory in directories]
    assert held[0], "the run has no session files, so the comparison above proved nothing"
    assert all(names == held[0] for names in held)

    # Steps b and c carry utterances, so they owe every item back as well as
    # every file. Steps d and e carry mistake records, where a different count
    # is the normal case and not a defect.
    assert compare_line_counts(directories[1], directories[2]) == []

    # An utterance the learner wrote none of keeps its line with empty text, so
    # the equal counts above are not equal because nothing was ever dropped.
    emptied = [
        utterance
        for path in session_files(directories[2])
        for utterance in read_session(path)
        if not utterance.text.strip()
    ]
    assert emptied, "the fixture must exercise an item step c empties"

    # The negative control: with a file removed the comparison names it, so the
    # silence above is a passing check rather than a comparison of two empties.
    (directories[-1] / sorted(held[-1])[0]).unlink()
    dropped = compare_file_sets(directories[-2], directories[-1])
    assert [diagnostic.item_ref for diagnostic in dropped] == [sorted(held[-1])[0]]


# -- what the review refuses ------------------------------------------------


def test_the_review_refuses_a_run_whose_mistakes_double_count(workspace: Workspace) -> None:
    """``verified_total_mistakes`` is the number of step-d records.

    Two records over the same characters count one mistake twice, which inflates
    the learner's error rate with nothing further down able to tell. The step-d
    driver checks it, and so does the review: a rule only the producer enforces
    holds right up until someone edits a promoted file and never reruns the
    producer, which is exactly what this does.
    """
    run_id = _start(workspace)
    _advance_through(workspace, run_id, StepId.E_VERIFIED)

    step_c = step_dir(run_id, StepId.C_AUTHORED, root=workspace.runs_root)
    target = next(
        (path, utterance)
        for path in session_files(step_c)
        for utterance in read_session(path)
        if len(utterance.text) > 20
    )
    path, utterance = target
    wide = MistakeRecord(
        utterance_id=utterance.utterance_id,
        evidence_span=EvidenceSpan(start=0, end=20),
        mistake="A construction that needs rewriting.",
        rule="A rule about that construction.",
        example="A natural rewriting of it.",
        example_type=ExampleType.SYNTHETIC,
        source_type=utterance.source_adapter,
        modality=utterance.modality,
    )
    nested = wide.model_copy(update={"evidence_span": EvidenceSpan(start=5, end=12)})
    # Written into both steps, so the only thing wrong with this run is the
    # double count itself and not a step-e file that disagrees with step d.
    for step in (StepId.D_MISTAKES, StepId.E_VERIFIED):
        write_jsonl_models(
            step_dir(run_id, step, root=workspace.runs_root) / path.name, [wide, nested]
        )

    with pytest.raises(ValueError, match="fail their own verifier") as excinfo:
        build_review.build_review(run_id, runs_root=workspace.runs_root)
    assert "CARDINALITY_MISMATCH" in str(excinfo.value)


def test_the_review_reports_the_utterances_it_could_not_judge(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """Coverage loss must not hide inside a denominator.

    A session step c quarantines never becomes eligible, so it is absent from
    every count the review shows. A run that failed to judge half its input
    would otherwise report a rate over the surviving half and say nothing at all
    about the rest.
    """
    run_id = _start(workspace)
    _advance_through(workspace, run_id, StepId.B_DEDUPLICATED)
    step_b = session_files(step_dir(run_id, StepId.B_DEDUPLICATED, root=workspace.runs_root))
    handed_to_step_c = sum(len(read_session(path)) for path in step_b)
    quarantined = step_b[-1]
    lost = len(read_session(quarantined))

    _advance_through(workspace, run_id, StepId.E_VERIFIED, spoil=quarantined.name)

    build_review.main(["--run-id", run_id, "--runs-root", str(workspace.runs_root)])
    reported = json.loads(capsys.readouterr().out)
    assert reported["unjudged_utterances"] == lost > 0
    # Every message step c was handed is now in exactly one of three places: it
    # is eligible, the learner wrote none of it, or nobody could judge it. Only
    # the first reaches a count the review publishes, so the arithmetic below is
    # what stops the third from being quietly absorbed into the denominator.
    assert reported["eligible_utterances"] > 0
    assert (
        reported["eligible_utterances"]
        + reported["non_authored_utterances"]
        + reported["unjudged_utterances"]
        == handed_to_step_c
    )


# -- one bad source never costs the others ---------------------------------


class _RealExceptVerify:
    """The real adapter, reporting an error about whatever it extracted.

    Every adapter's ``verify`` was dead code in production: several hundred
    lines across nine adapters, including the opened-path audits that emit
    ``SOURCE_SNAPSHOT_UNSAFE_PATH``, called only by tests. No structural defect
    an adapter could detect was able to fail a run.
    """

    def __init__(self) -> None:
        self._real = claude_code_adapter()

    @property
    def adapter_id(self) -> str:
        return self._real.adapter_id

    @property
    def adapter_version(self) -> str:
        return self._real.adapter_version

    @property
    def stability(self) -> Stability:
        return self._real.stability

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        return self._real.discover(context)

    def snapshot(
        self, instance: SourceInstanceRecord, source_path: Path, target_dir: Path
    ) -> SnapshotCapture:
        return self._real.snapshot(instance, source_path, target_dir)

    def extract(
        self, instance: SourceInstanceRecord, snapshot_dir: Path
    ) -> Iterator[NormalizedUtterance]:
        return self._real.extract(instance, snapshot_dir)

    def verify(
        self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
    ) -> list[Diagnostic]:
        return [
            Diagnostic.from_code(
                "SOURCE_SNAPSHOT_UNSAFE_PATH",
                "the adapter opened a file outside its allowlist",
                item_ref="synthetic",
            )
        ]


def _collect_with_a_failing_adapter(workspace: Workspace, run_id: str) -> dict[str, object]:
    """Swap the factory for the run and restore it, whatever happens."""
    previous = registry._FACTORIES["claude_code"]
    registry._FACTORIES["claude_code"] = _RealExceptVerify
    try:
        return collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    finally:
        registry._FACTORIES["claude_code"] = previous


def test_an_adapter_that_fails_its_own_checks_loses_only_its_own_source(
    workspace: Workspace,
) -> None:
    run_id = _start(workspace)

    collected = _collect_with_a_failing_adapter(workspace, run_id)

    assert _count(collected, "candidate_utterances") == 0
    entries = collected["excluded_instances"]
    assert isinstance(entries, list)
    assert entries and all("SOURCE_SNAPSHOT_UNSAFE_PATH" in entry["reason"] for entry in entries)


def test_a_source_that_fails_verification_is_reported_once(workspace: Workspace) -> None:
    """One instance, one exclusion entry.

    The failure path deletes the snapshot and then records the exclusion. An
    earlier ordering recorded it first, so a cleanup that raised produced two
    entries for the same instance and a count that overstated how many sources
    the user lost.
    """
    run_id = _start(workspace)

    collected = _collect_with_a_failing_adapter(workspace, run_id)

    entries = collected["excluded_instances"]
    assert isinstance(entries, list)
    instances = [entry["instance"] for entry in entries]
    assert len(instances) == len(set(instances)), f"an instance was excluded twice: {instances}"
