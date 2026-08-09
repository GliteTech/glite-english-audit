"""Interruption and resumption across the five steps (specification, 13.5).

The checkpoint unit is the session file. That is the whole change: a run used to
be interrupted somewhere inside a pooled batch, and now it is interrupted with
some session files judged and the rest not. ``--prepare`` reports which files are
already written, so a resumed run asks only for what is missing rather than
paying for every judgment again — and the tests below hold the drivers to that
rather than to the manifest alone.

Every test interrupts a real run and continues it from the run directory alone,
because that is the only thing a resumed audit may depend on: the user continues
in a fresh agent conversation, so nothing may be carried in conversation history
or in process memory (specification, 9.4). One test therefore resumes in a
genuinely fresh interpreter, as ``tests/test_pipeline_cli_subprocess.py`` does.

Steps a and b are scripts. Steps c, d and e are one agent per session file, and
the agent is the only thing simulated here: a fixed judgment stands in for the
model, and the driver on either side of it is the real one, including the
promotion. The orchestration between steps is simulated too, in ``_resume`` —
which is where the one thing the skill owns and the code does not lives: after an
invalidating change, the invalidated step's files must be cleared, or
``already_written`` hands the resumed pass exactly the judgments the change was
meant to replace.

The run directory sits under the checkout's ``runtime/`` tree, so snapshots land
inside the run directory as they do in production and retention is tested against
the real layout.
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.adapters.claude_code.adapter import ClaudeCodeAdapter
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
)
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, RunManifest
from glite_english_audit.artifacts.models import NormalizedUtterance, SourceInstanceRecord
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline import (
    authorship,
    build_review,
    collect,
    deduplicate,
    mistakes,
    start_run,
    verify,
)
from glite_english_audit.pipeline.agent_io import (
    AuthoredLine,
    DropList,
    MistakeDraft,
    RecordForConfidentiality,
    UtteranceForJudgment,
    decision_path,
    projection_path,
    verdict_path,
)
from glite_english_audit.pipeline.authorship import INDEX_NAME as CORPUS_INDEX_NAME
from glite_english_audit.pipeline.authorship import AuthoredCorpusIndex, read_repair_list
from glite_english_audit.sessions import read_session, session_files
from glite_english_audit.state.event_log import log_event, read_events
from glite_english_audit.state.machine import advance_run
from glite_english_audit.state.run_store import (
    EARLIEST_CLIENT_CODE_STEP,
    EARLIEST_SEMANTIC_STEP,
    RUN_MANIFEST_FILENAME,
    ResumeAssessment,
    ResumeDecision,
    RunStoreError,
    cleanup_completed,
    describe_resume,
    expire_stale_runs,
    invalidate_from,
    list_unfinished,
    load_manifest,
    next_incomplete_step,
    save_manifest,
    write_checkpoint,
)

_REPO = Path(__file__).resolve().parent.parent
_FIXTURE_HOME = _REPO / "fixtures" / "claude_code" / "success" / "home"

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=3)
_INVENTORY_NAME = "source-inventory.json"

# One fixed judgment per construction the synthetic fixture plants, standing in
# for the model (specification, 7.1): the phrase, then the three shareable
# sentences a step-d agent writes about it. The examples are invented rather
# than quoted, which is what step d owes.
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

# The one record a step-e agent withholds, so the withheld count has something
# real to preserve across a resume.
_WITHHELD = _MARKED[0][1]

_INVENTED_SPAN = "a sentence the learner never wrote"


@dataclass(frozen=True)
class Workspace:
    """A checkout, a source home, and the run store inside the checkout."""

    repo: Path
    home: Path
    inventory_dir: Path
    runs_root: Path


@dataclass(frozen=True)
class Resumed:
    """What one resume attempt decided, and what it recomputed."""

    assessment: ResumeAssessment
    steps_run: tuple[StepId, ...]
    judged: tuple[str, ...]
    """Session files the step-c agents were asked for on this pass."""


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    home = tmp_path / "home"
    shutil.copytree(_FIXTURE_HOME, home)
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
    _write_inventory(home, inventory_dir / _INVENTORY_NAME)
    return Workspace(
        repo=repo,
        home=home,
        inventory_dir=inventory_dir,
        runs_root=repo / "runtime" / "runs",
    )


def _write_inventory(home: Path, target: Path, *, now: datetime = _NOW) -> None:
    """Discover ``home`` and write the inventory to the file ``target`` names."""
    outcome = claude_code_adapter().discover(
        DiscoveryContext(os_environment=OsEnvironment.MACOS, home=home, now=now, environ={})
    )
    assert outcome.records, "discovery must find the fixture instances"
    ensure_private_dir(target.parent)
    write_model(
        target,
        PrivateInventory(
            records=outcome.records,
            instance_paths={key: str(path) for key, path in outcome.instance_paths.items()},
            created_at=now,
        ),
    )


def _start(workspace: Workspace, *, now: datetime = _NOW) -> RunManifest:
    return start_run.start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=workspace.runs_root,
        inventory_dir=workspace.inventory_dir,
        local_scan_consent=True,
        provider_transfer_consent=True,
        now=now,
    )


def _run_directory(workspace: Workspace, run_id: str) -> Path:
    return workspace.runs_root / run_id


def _step(workspace: Workspace, run_id: str, step: StepId) -> Path:
    return step_dir(run_id, step, root=workspace.runs_root)


def _step_files(workspace: Workspace, run_id: str, step: StepId) -> dict[str, bytes]:
    root = _step(workspace, run_id, step)
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# -- the agents each step hands its session files to -----------------------


def _authored(text: str) -> str:
    """The spans of one message the learner actually wrote.

    Fixed rather than clever: a message about the deploy script is treated as
    pasted and comes back with its text emptied, everything else is the
    learner's own. An emptied item keeps its line, because a vanished item and
    an unauthored one mean different things.
    """
    return "" if _PASTED in text else text


def _judge_authorship(
    workspace: Workspace, run_id: str, *, limit: int | None = None, spoil: str | None = None
) -> tuple[str, ...]:
    """Run step c's ``--prepare`` and answer the files it asks for.

    Returns the files this pass judged. ``limit`` stops after that many, which
    is how an interruption inside step c is produced; ``spoil`` names a file
    whose judgment claims a span the step-b text does not contain, which is the
    failure the deterministic span scan must quarantine.
    """
    prepared = authorship.prepare(run_id, runs_root=workspace.runs_root)
    judged: list[str] = []
    for session in prepared.sessions:
        # Already on disk means judged before the interruption. Asking for it
        # again is the cost this whole design exists to avoid.
        if session.already_written:
            continue
        if limit is not None and len(judged) >= limit:
            break
        # A projection in and a decision out: the agent is handed an index, a
        # modality and the text, and answers with the index and the spans it
        # kept. The driver's --apply turns those into the session artifact.
        decisions = [
            AuthoredLine(i=item.i, text=_authored(item.text))
            for item in read_jsonl_models(Path(session.input_path), UtteranceForJudgment)
        ]
        if spoil == session.file_name:
            decisions[0] = decisions[0].model_copy(update={"text": _INVENTED_SPAN})
        write_jsonl_models(Path(session.output_path), decisions)
        judged.append(session.file_name)
    return tuple(judged)


def _offered(assignment: mistakes.SessionAssignment) -> list[UtteranceForJudgment]:
    """The utterances one step-d agent is handed, numbered as the driver numbers them."""
    return list(read_jsonl_models(Path(assignment.read), UtteranceForJudgment))


def _drafts_for(offered: list[UtteranceForJudgment]) -> list[MistakeDraft]:
    """One draft per marked construction in one session's projection.

    A draft says which line and which characters, never whose utterance: the
    identity and provenance a record carries are the driver's to re-derive, so
    a judgment that got them wrong is not a failure this fake can simulate.
    """
    drafts: list[MistakeDraft] = []
    for item in offered:
        for phrase, what, rule, example in _MARKED:
            start = item.text.find(phrase)
            if start < 0:
                continue
            drafts.append(
                MistakeDraft(
                    i=item.i,
                    span=(start, start + len(phrase)),
                    mistake=what,
                    rule=rule,
                    example=example,
                    example_type=ExampleType.SYNTHETIC,
                )
            )
    return drafts


def _write_mistakes(workspace: Workspace, run_id: str) -> None:
    for assignment in mistakes.prepare_mistakes(run_id, runs_root=workspace.runs_root):
        write_jsonl_models(Path(assignment.write), _drafts_for(_offered(assignment)))


def _confirm_mistakes(workspace: Workspace, run_id: str) -> None:
    """The indices step e withholds. It may drop, and a drop list can do nothing else."""
    source = _step(workspace, run_id, StepId.D_MISTAKES)
    target = _step(workspace, run_id, StepId.E_VERIFIED)
    for path in session_files(source):
        # Read from the projection, which is all a step-e agent is given: the
        # published face of each record, without the addresses it must not judge.
        offered = read_jsonl_models(projection_path(target, path.name), RecordForConfidentiality)
        write_model(
            verdict_path(target, path.name),
            DropList(drop=[record.i for record in offered if record.mistake == _WITHHELD]),
        )


def _produce(workspace: Workspace, run_id: str, step: StepId) -> tuple[str, ...]:
    """Run one whole step: its ``--prepare``, its agents, and its ``--apply``.

    Returns the session files the agents were asked for, which only step c
    reports and only step c can partly skip.
    """
    if step is StepId.A_COLLECTED:
        collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    elif step is StepId.B_DEDUPLICATED:
        deduplicate.deduplicate(run_id, runs_root=workspace.runs_root)
    elif step is StepId.C_AUTHORED:
        judged = _judge_authorship(workspace, run_id)
        authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
        return judged
    elif step is StepId.D_MISTAKES:
        _write_mistakes(workspace, run_id)
        mistakes.apply_mistakes(run_id, runs_root=workspace.runs_root)
    else:
        _confirm_mistakes(workspace, run_id)
        verify.apply_verification(run_id, runs_root=workspace.runs_root)
    return ()


# -- the bookkeeping the orchestration performs ----------------------------


def _advance_through(
    workspace: Workspace, run_id: str, last: StepId, *, at: datetime = _NOW
) -> RunManifest:
    """Run every step up to ``last``, then date the checkpoint at ``at``.

    The drivers write their own checkpoints; redating the last one is how these
    tests move the retention clock without pretending a step ran.
    """
    manifest = load_manifest(run_id, root=workspace.runs_root)
    for step in StepId:
        if step > last:
            break
        if manifest.steps[step].status is StepStatus.PROMOTED:
            continue
        _produce(workspace, run_id, step)
    manifest = load_manifest(run_id, root=workspace.runs_root)
    return write_checkpoint(manifest, root=workspace.runs_root, now=at)


def _refresh_preflight(manifest: RunManifest) -> None:
    """Walk the run back through a preflight, as the resume policy requires."""
    if manifest.status is RunStatus.PROCESSING:
        manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    if manifest.status is not RunStatus.AWAITING_PREFLIGHT:
        manifest.status = advance_run(manifest.status, RunStatus.AWAITING_PREFLIGHT)
    manifest.status = advance_run(manifest.status, RunStatus.PROCESSING)


def _clear_invalidated(workspace: Workspace, run_id: str, earliest: StepId) -> None:
    """Delete the session files of every invalidated step.

    The manifest change alone does not reach the disk, and ``--prepare`` reports
    a file that is still there as ``already_written``. A resume that skipped
    this would re-verify the very judgments the changed skill was supposed to
    replace and report the step recomputed. The skill says it in words —
    reprocess promoted files when their required versions changed — and this is
    what saying it costs.
    """
    for step in StepId:
        if step < earliest:
            continue
        for path in session_files(_step(workspace, run_id, step)):
            path.unlink()


def _resume(
    workspace: Workspace,
    run_id: str,
    current: CompatibilityFingerprint,
    *,
    at: datetime = _LATER,
) -> Resumed:
    """Continue a run from the manifest on disk, as a fresh conversation must.

    Nothing from the interrupted pass is passed in: the manifest is reread, the
    policy is applied to it, and work restarts at the step the policy names.
    """
    manifest = load_manifest(run_id, root=workspace.runs_root)
    assessment = describe_resume(manifest, current, now=at)
    if assessment.decision in (ResumeDecision.RESTART, ResumeDecision.EXPIRED):
        return Resumed(assessment=assessment, steps_run=(), judged=())
    if assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM:
        earliest = assessment.earliest_affected_step
        assert earliest is not None
        invalidate_from(manifest, earliest, now=at)
        manifest.fingerprint = current
        _refresh_preflight(manifest)
        save_manifest(manifest, root=workspace.runs_root)
        _clear_invalidated(workspace, run_id, earliest)

    started = next_incomplete_step(manifest)
    ran: list[StepId] = []
    judged: tuple[str, ...] = ()
    if started is not None:
        for step in StepId:
            if step < started:
                continue
            judged += _produce(workspace, run_id, step)
            ran.append(step)
    write_checkpoint(
        load_manifest(run_id, root=workspace.runs_root), root=workspace.runs_root, now=at
    )
    return Resumed(assessment=assessment, steps_run=tuple(ran), judged=judged)


def _changed(
    fingerprint: CompatibilityFingerprint, **overrides: object
) -> CompatibilityFingerprint:
    return fingerprint.model_copy(update=overrides)


# -- the run never completed ------------------------------------------------


def test_a_run_interrupted_before_its_manifest_offers_nothing_to_resume(
    workspace: Workspace,
) -> None:
    # start_run writes the run's inventory copy before the manifest, so a crash
    # between them leaves a directory holding private data and no state file. It
    # must not be offered for resume, and it must not break the listing.
    manifest = _start(workspace)
    (_run_directory(workspace, manifest.run_id) / RUN_MANIFEST_FILENAME).unlink()

    assert list_unfinished(workspace.runs_root, now=_NOW) == []


# -- interrupted between two steps ------------------------------------------


@pytest.mark.parametrize("step", list(StepId), ids=lambda step: step.name.lower())
def test_the_step_whose_driver_never_ran_is_where_the_resume_starts(
    workspace: Workspace, step: StepId
) -> None:
    """Every step in turn is the one the process died before reaching."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    previous = StepId(step - 1) if step > StepId.A_COLLECTED else None
    if previous is not None:
        _advance_through(workspace, run_id, previous, at=_NOW)
    else:
        write_checkpoint(manifest, root=workspace.runs_root, now=_NOW)
    upstream = {
        earlier: _step_files(workspace, run_id, earlier)
        for earlier in StepId
        if previous is not None and earlier <= previous
    }

    reread = load_manifest(run_id, root=workspace.runs_root)
    assert reread.last_checkpoint_at == _NOW
    assert next_incomplete_step(reread) is step

    resumed = _resume(workspace, run_id, reread.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.steps_run[0] is step
    assert resumed.steps_run[-1] is StepId.E_VERIFIED
    # Nothing earlier was touched: the interruption cost work, never data.
    for earlier, before in upstream.items():
        assert _step_files(workspace, run_id, earlier) == before
    final = load_manifest(run_id, root=workspace.runs_root)
    assert next_incomplete_step(final) is None
    assert final.last_checkpoint_at == _LATER


@pytest.mark.parametrize("step", list(StepId), ids=lambda step: step.name.lower())
def test_a_checkpointed_step_is_never_reprocessed(workspace: Workspace, step: StepId) -> None:
    """Everything through ``step`` is promoted and checkpointed."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    manifest = _advance_through(workspace, run_id, step, at=_NOW)
    # A clean stop: the run checkpointed and the process ended.
    manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    save_manifest(manifest, root=workspace.runs_root)

    done = [earlier for earlier in StepId if earlier <= step]
    before = {earlier: _step_files(workspace, run_id, earlier) for earlier in done}
    identifiers = {
        earlier: manifest.steps[earlier].current_artifact_id
        for earlier in done
        if manifest.steps[earlier].current_artifact_id is not None
    }
    assert identifiers or step < StepId.C_AUTHORED

    resumed = _resume(workspace, run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert set(resumed.steps_run).isdisjoint(done)
    for earlier in done:
        assert _step_files(workspace, run_id, earlier) == before[earlier]
    final = load_manifest(run_id, root=workspace.runs_root)
    for earlier, artifact_id in identifiers.items():
        assert final.steps[earlier].current_artifact_id == artifact_id
    assert next_incomplete_step(final) is None
    # A resume that found work is processing again; one that found none leaves
    # the run exactly where the clean stop left it.
    expected = RunStatus.PROCESSING if resumed.steps_run else RunStatus.CHECKPOINTED
    assert final.status is expected


# -- interrupted inside a step: the file is the checkpoint unit -------------


def test_a_resumed_step_c_asks_only_for_the_session_files_it_is_missing(
    workspace: Workspace,
) -> None:
    """The session file is the checkpoint unit (specification, 9.3).

    A judgment already on disk is paid for, so the resumed pass must not buy it
    again — and it must not skip the rest, which is the failure that looks
    identical from the manifest and costs the run half its corpus.
    """
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.B_DEDUPLICATED, at=_NOW)

    first = authorship.prepare(run_id, runs_root=workspace.runs_root)
    assert len(first.sessions) > 1
    assert [session.already_written for session in first.sessions] == [False] * len(first.sessions)
    # The agents got through the first file and the process stopped before
    # anything applied their work.
    done = _judge_authorship(workspace, run_id, limit=1)
    assert len(done) == 1
    # The judgment that was paid for is the decision file, not the artifact:
    # nothing has applied yet, so the artifact does not exist and could not tell
    # a reused judgment from a repurchased one.
    finished = decision_path(_step(workspace, run_id, StepId.C_AUTHORED), done[0])
    paid_for = finished.read_bytes()

    reported = {
        session.file_name: session.already_written
        for session in authorship.prepare(run_id, runs_root=workspace.runs_root).sessions
    }
    assert reported[done[0]] is True
    missing = sorted(name for name, written in reported.items() if not written)
    assert missing == sorted(set(reported) - set(done))

    reread = load_manifest(run_id, root=workspace.runs_root)
    assert next_incomplete_step(reread) is StepId.C_AUTHORED
    resumed = _resume(workspace, run_id, reread.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.steps_run[0] is StepId.C_AUTHORED
    assert sorted(resumed.judged) == missing
    assert finished.read_bytes() == paid_for, "a finished judgment was asked for twice"
    # And the corpus is whole: skipping the finished file did not drop it.
    index = read_model(
        _step(workspace, run_id, StepId.C_AUTHORED) / CORPUS_INDEX_NAME, AuthoredCorpusIndex
    )
    assert index.session_count == len(first.sessions)
    assert index.quarantined_session_count == 0
    assert index.utterance_count == first.utterance_count


def test_a_repair_pass_asks_for_exactly_the_sessions_that_failed(workspace: Workspace) -> None:
    """A quarantined judgment is repairable, not lost (specification, 6.4).

    Its words leave the denominator, and until something read the repair list
    there was no way to get them back short of redoing the whole run.
    """
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.B_DEDUPLICATED, at=_NOW)
    files = [path.name for path in session_files(_step(workspace, run_id, StepId.B_DEDUPLICATED))]
    spoiled = files[-1]

    _judge_authorship(workspace, run_id, spoil=spoiled)
    applied = authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
    assert applied.sessions_quarantined == 1
    assert [diagnostic.code for diagnostic in applied.diagnostics] == [
        "AUTHORSHIP_SPAN_NOT_VERBATIM"
    ]
    assert read_repair_list(run_id, runs_root=workspace.runs_root) == [spoiled]

    repair = authorship.prepare(run_id, runs_root=workspace.runs_root, repair_only=True)

    # Exactly the failed session, and it is asked for rather than reported as
    # already written. A repair pass exists to replace the decision that failed,
    # so a decision left on disk for reuse is the one outcome that turns the
    # repair into a no-op and loses the session's words for good.
    assert [session.file_name for session in repair.sessions] == [spoiled]
    assert repair.sessions[0].already_written is False
    _judge_authorship(workspace, run_id)
    repaired = authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
    assert repaired.sessions_quarantined == 0
    assert repaired.sessions_verified == len(files)
    assert read_repair_list(run_id, runs_root=workspace.runs_root) == []


def test_a_quarantined_step_is_repaired_by_the_resume_rather_than_redone(
    workspace: Workspace,
) -> None:
    """A step the driver refused keeps its diagnostics and re-enters work.

    A step that raised and left its status at ``in_progress`` is
    indistinguishable from one still running; naming the failure is what lets a
    resume repair it instead of trusting it.
    """
    manifest = _start(workspace)
    run_id = manifest.run_id
    run_directory = _run_directory(workspace, run_id)
    _advance_through(workspace, run_id, StepId.C_AUTHORED, at=_NOW)

    # One step-d file cites a span past the end of the utterance it addresses,
    # which is the check that stops a record from quoting text nobody wrote.
    assignments = mistakes.prepare_mistakes(run_id, runs_root=workspace.runs_root)
    for assignment in assignments:
        offered = _offered(assignment)
        drafts = _drafts_for(offered)
        if assignment is assignments[0]:
            target = next(item for item in offered if item.text.strip())
            drafts = [
                MistakeDraft(
                    i=target.i,
                    span=(0, len(target.text) + 50),
                    mistake=_MARKED[0][1],
                    rule=_MARKED[0][2],
                    example=_MARKED[0][3],
                    example_type=ExampleType.SYNTHETIC,
                )
            ]
        write_jsonl_models(Path(assignment.write), drafts)
    outcome = mistakes.apply_mistakes(run_id, runs_root=workspace.runs_root)
    assert not outcome.passed
    assert "SCHEMA_INVALID_VALUE" in {diagnostic.code for diagnostic in outcome.diagnostics}
    for diagnostic in outcome.diagnostics:
        log_event(
            run_directory,
            "item_quarantined",
            step_id=StepId.D_MISTAKES,
            diagnostic_codes=[diagnostic.code],
        )
    write_checkpoint(
        load_manifest(run_id, root=workspace.runs_root), root=workspace.runs_root, now=_NOW
    )

    # A fresh read of the run: the quarantine and its history are still there.
    reread = load_manifest(run_id, root=workspace.runs_root)
    assert reread.steps[StepId.D_MISTAKES].status is StepStatus.QUARANTINED
    assert next_incomplete_step(reread) is StepId.D_MISTAKES

    resumed = _resume(workspace, run_id, reread.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.steps_run == (StepId.D_MISTAKES, StepId.E_VERIFIED)
    assert (
        load_manifest(run_id, root=workspace.runs_root).steps[StepId.D_MISTAKES].status
        is StepStatus.PROMOTED
    )
    # The repair does not erase what the failed pass recorded.
    assert {event.kind for event in read_events(run_directory)} == {"item_quarantined"}
    # The log is content-free by construction; prove it over the real bytes.
    log_bytes = (run_directory / "logs" / "events.jsonl").read_bytes()
    for phrase in (b"I very like", b"deploy script", b"learning progress"):
        assert phrase not in log_bytes


# -- resumption in a fresh process -----------------------------------------


_RESUME_PROBE = """
import json
import sys
from datetime import datetime
from pathlib import Path

from glite_english_audit.state.run_store import (
    describe_resume,
    list_unfinished,
    load_manifest,
    next_incomplete_step,
)

runs_root = Path(sys.argv[1])
run_id = sys.argv[2]
moment = datetime.fromisoformat(sys.argv[3])
manifest = load_manifest(run_id, root=runs_root)
assessment = describe_resume(manifest, manifest.fingerprint, now=moment)
step = next_incomplete_step(manifest)
print(
    json.dumps(
        {
            "decision": assessment.decision.value,
            "detail": assessment.detail,
            "next_step": None if step is None else int(step),
            "unfinished": [
                summary.run_id for summary in list_unfinished(runs_root, now=moment)
            ],
        }
    )
)
"""


def _fresh_process(script: Path, *args: str, cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(script), *args], cwd=cwd, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return dict(json.loads(result.stdout))


def _driver_process(module: str, *args: str, cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", module, *args], cwd=cwd, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{module} failed: {result.stderr[-2000:]}"
    return dict(json.loads(result.stdout))


def test_resume_works_in_a_fresh_process(workspace: Workspace, tmp_path: Path) -> None:
    """Resumption may not depend on the conversation or on process state."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    manifest = _advance_through(workspace, run_id, StepId.D_MISTAKES, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    save_manifest(manifest, root=workspace.runs_root)

    probe = tmp_path / "resume_probe.py"
    probe.write_text(_RESUME_PROBE, encoding="utf-8")
    # cwd is deliberately outside the checkout: a resumed run reads the run
    # store, never the working directory it was started from. The clock is
    # passed in, so the assertions below do not age.
    payload = _fresh_process(
        probe, str(workspace.runs_root), run_id, _LATER.isoformat(), cwd=tmp_path
    )

    assert payload["decision"] == ResumeDecision.CONTINUE.value
    assert payload["next_step"] == int(StepId.E_VERIFIED)
    assert payload["unfinished"] == [run_id]

    # The step-e agents run in this process because they are the model's part.
    # Their driver, and the review after it, do not.
    _confirm_mistakes(workspace, run_id)
    confirmed = _driver_process(
        "glite_english_audit.pipeline.verify",
        "--run-id",
        run_id,
        "--apply",
        "--runs-root",
        str(workspace.runs_root),
        cwd=tmp_path,
    )
    # The review is not a step, so it is not what the resume continued into; it
    # is the run's last local act, and it still has to work from the run
    # directory alone.
    reviewed = _driver_process(
        "glite_english_audit.pipeline.build_review",
        "--run-id",
        run_id,
        "--runs-root",
        str(workspace.runs_root),
        cwd=tmp_path,
    )

    assert confirmed["records_dropped"] == 1
    assert reviewed["verified_total_mistakes"] == len(_MARKED)
    assert reviewed["records"] == reviewed["shared_mistakes"] == len(_MARKED) - 1
    assert reviewed["withheld_for_privacy"] == 1
    # Driver output reaches the agent conversation, so it carries counts only.
    blob = json.dumps([confirmed, reviewed, payload])
    for phrase in ("I very like", "I am agree", "learning progress"):
        assert phrase not in blob


# -- the frozen record cutoff ----------------------------------------------


def test_a_record_after_the_cutoff_belongs_to_the_next_audit(workspace: Workspace) -> None:
    """New source records never invalidate a resume (specification, 13.5)."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.A_COLLECTED, at=_NOW)
    selection = manifest.selection
    assert selection is not None
    assert selection.record_cutoff_at == _NOW
    collected = sum(
        len(read_session(path))
        for path in session_files(_step(workspace, run_id, StepId.A_COLLECTED))
    )
    before = _step_files(workspace, run_id, StepId.A_COLLECTED)

    transcript = (
        workspace.home
        / ".claude/projects/-home-tester-notes/22222222-2222-4222-8222-222222222222.jsonl"
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "uuid": "later-1",
                    "parentUuid": None,
                    "timestamp": (_NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    "sessionId": "22222222-2222-4222-8222-222222222222",
                    "cwd": "/home/tester/notes",
                    "version": "2.1.210",
                    "gitBranch": "main",
                    "userType": "external",
                    "entrypoint": "cli",
                    "isSidechain": False,
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Tomorrow I will writing one more note.",
                    },
                }
            )
            + "\n"
        )

    resumed = _resume(workspace, run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    # Every session file step a wrote is byte for byte what it was: the resume
    # continued from step b and never re-read the source.
    assert _step_files(workspace, run_id, StepId.A_COLLECTED) == before

    # The record is real and in period: a later audit, with a later cutoff,
    # picks it up. That is what makes the exclusion above a cutoff decision.
    # A later audit rediscovers first, and it has to: the inventory records how
    # many candidate messages each instance held, and the adapter's own verify()
    # fails an instance that now yields a different number. Reusing the previous
    # audit's inventory is what makes a new message look like a defect.
    later = _NOW + timedelta(days=2)
    _write_inventory(workspace.home, workspace.inventory_dir / _INVENTORY_NAME, now=later)
    next_audit = _start(workspace, now=later)
    counts = collect.collect(next_audit.run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    assert counts["excluded_instances"] == []
    assert counts["candidate_utterances"] == collected + 1


# -- the four resume decisions ---------------------------------------------


def test_resume_continue_finishes_the_remaining_steps(workspace: Workspace) -> None:
    manifest = _start(workspace)
    _advance_through(workspace, manifest.run_id, StepId.C_AUTHORED, at=_NOW)

    resumed = _resume(workspace, manifest.run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.steps_run == (StepId.D_MISTAKES, StepId.E_VERIFIED)
    reviewed = build_review.build_review(manifest.run_id, runs_root=workspace.runs_root)
    counts = reviewed.counts
    assert counts.shared_mistakes + counts.withheld_for_privacy == counts.verified_total_mistakes


def test_a_changed_skill_recomputes_from_the_first_step_a_model_produces(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.E_VERIFIED, at=_NOW)
    # Steps a and b are the whole deterministic prefix: collection is a script
    # and so is deduplication, so no skill, prompt or model change can reach
    # either of them.
    kept = {
        step: _step_files(workspace, run_id, step)
        for step in StepId
        if step <= StepId.B_DEDUPLICATED
    }
    deduplicated_artifact = manifest.steps[StepId.B_DEDUPLICATED].current_artifact_id

    changed = _changed(manifest.fingerprint, skill_versions={"find-english-mistakes": 2})
    resumed = _resume(workspace, run_id, changed)

    assert resumed.assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    # The skill that changed is step d's, but the fingerprint holds one map of
    # skill versions for the whole run rather than one entry per step, so the
    # policy cannot attribute a change to the step that owns it. It takes the
    # conservative reading and recomputes from the first step a model produces.
    assert resumed.assessment.earliest_affected_step is EARLIEST_SEMANTIC_STEP
    assert resumed.steps_run == (StepId.C_AUTHORED, StepId.D_MISTAKES, StepId.E_VERIFIED)
    # And every session file of the invalidated step was judged again. A resume
    # that reported the step recomputed while reusing the old judgments would
    # satisfy every assertion above and none of this one.
    assert sorted(resumed.judged) == sorted(
        path.name for path in session_files(_step(workspace, run_id, StepId.C_AUTHORED))
    )
    for step, before in kept.items():
        assert _step_files(workspace, run_id, step) == before
    final = load_manifest(run_id, root=workspace.runs_root)
    assert final.steps[StepId.B_DEDUPLICATED].current_artifact_id == deduplicated_artifact
    assert final.fingerprint == changed
    assert all(
        final.steps[step].status is StepStatus.PROMOTED
        for step in StepId
        if step >= StepId.C_AUTHORED
    )


@pytest.mark.parametrize(
    ("overrides", "earliest"),
    [
        ({"skill_versions": {"filter-authored-english": 3}}, EARLIEST_SEMANTIC_STEP),
        ({"prompt_versions": {"find-mistakes": 2}}, EARLIEST_SEMANTIC_STEP),
        ({"model_ids": {"find-mistakes": "example-model-2"}}, EARLIEST_SEMANTIC_STEP),
        ({"client_version": "99.0.0"}, EARLIEST_CLIENT_CODE_STEP),
    ],
    ids=["skill", "prompt", "model", "client"],
)
def test_resume_recomputes_from_the_earliest_step_the_change_can_reach(
    workspace: Workspace, overrides: dict[str, object], earliest: StepId
) -> None:
    # The first three describe how a model is asked, so they land on the first
    # step a model produces. The fourth is pure Python, and it lands one step
    # later because that is where client code starts deciding what may be
    # shared: the privacy scanner and the packaging allowlist (specification,
    # 6.6, 8.3). A run checkpointed before a scanner fix must not resume with
    # records the known-bad scanner approved.
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.E_VERIFIED, at=_NOW)
    untouched = {step: _step_files(workspace, run_id, step) for step in StepId if step < earliest}

    resumed = _resume(workspace, run_id, _changed(manifest.fingerprint, **overrides))

    assert resumed.assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert resumed.assessment.earliest_affected_step is earliest
    assert resumed.steps_run == tuple(step for step in StepId if step >= earliest)
    for step, before in untouched.items():
        assert _step_files(workspace, run_id, step) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"adapter_versions": {"claude_code": "99.0.0"}},
        # 99, not 2: MANIFEST_SCHEMA_VERSION is 2 now, and a "changed"
        # version equal to the current one tests nothing.
        {"artifact_schema_version": 99},
        {"tokenizer_version": "9.9.9"},
        {"consent_policy_version": "2"},
    ],
    ids=["adapter", "schema", "tokenizer", "consent"],
)
def test_resume_detects_an_incompatible_change(
    workspace: Workspace, overrides: dict[str, object]
) -> None:
    # No versioned deterministic artifact migration exists yet, so every one of
    # these changes takes the conservative branch: reuse nothing
    # (specification, 9.4).
    manifest = _start(workspace)
    _advance_through(workspace, manifest.run_id, StepId.C_AUTHORED, at=_NOW)

    resumed = _resume(workspace, manifest.run_id, _changed(manifest.fingerprint, **overrides))

    assert resumed.assessment.decision is ResumeDecision.RESTART
    assert resumed.steps_run == ()


def test_invalidate_from_clears_pointers_and_keeps_a_quarantined_step(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    _advance_through(workspace, manifest.run_id, StepId.E_VERIFIED, at=_NOW)
    manifest = load_manifest(manifest.run_id, root=workspace.runs_root)
    mistakes_state = manifest.steps[StepId.D_MISTAKES]
    mistakes_state.status = StepStatus.QUARANTINED

    invalidated = invalidate_from(manifest, StepId.D_MISTAKES, now=_LATER)

    # A quarantined step keeps its status, so its diagnostic history is not
    # overwritten by the invalidation decision. Only the promoted step after it
    # moves, which is why the invalidated list names e and not d.
    assert invalidated == [StepId.E_VERIFIED]
    assert manifest.steps[StepId.D_MISTAKES].status is StepStatus.QUARANTINED
    downstream = manifest.steps[StepId.E_VERIFIED]
    assert downstream.status is StepStatus.INVALIDATED
    assert downstream.current_artifact_id is None
    assert downstream.current_artifact_hash is None
    assert manifest.steps[StepId.C_AUTHORED].status is StepStatus.PROMOTED
    assert manifest.steps[StepId.C_AUTHORED].current_artifact_id is not None


def test_resume_restart_reuses_nothing_and_leaves_the_old_run_alone(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.C_AUTHORED, at=_NOW)
    before = {step: _step_files(workspace, run_id, step) for step in StepId}

    resumed = _resume(workspace, run_id, _changed(manifest.fingerprint, tokenizer_version="9.9.9"))

    assert resumed.assessment.decision is ResumeDecision.RESTART
    assert resumed.steps_run == ()
    for step in StepId:
        assert _step_files(workspace, run_id, step) == before[step]
    unchanged = load_manifest(run_id, root=workspace.runs_root)
    assert unchanged.steps[StepId.C_AUTHORED].status is StepStatus.PROMOTED

    # A restart is a new run: the old one stays listed until retention or the
    # user's own decision removes it.
    fresh = _start(workspace, now=_LATER)
    assert {summary.run_id for summary in list_unfinished(workspace.runs_root, now=_LATER)} == {
        run_id,
        fresh.run_id,
    }


def test_resume_expired_after_the_retention_limit(workspace: Workspace) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.C_AUTHORED, at=_NOW)
    expired_moment = _NOW + timedelta(days=30, seconds=1)

    resumed = _resume(workspace, run_id, manifest.fingerprint, at=expired_moment)

    assert resumed.assessment.decision is ResumeDecision.EXPIRED
    assert "new audit" in resumed.assessment.detail
    assert resumed.steps_run == ()

    assert expire_stale_runs(workspace.runs_root, now=expired_moment) == [run_id]
    run_directory = _run_directory(workspace, run_id)
    # The state file is kept and every private artifact is deleted. Naming the
    # survivors rather than a list of directories is what keeps this true of the
    # layout rather than of one version of it: when the nine steps became five
    # steps, the run's inventory copy and the snapshot manifests moved out of the
    # step tree and up to the run root, and the inventory names the user's
    # applications and the absolute paths they keep their data under.
    assert sorted(
        str(path.relative_to(run_directory)) for path in run_directory.rglob("*") if path.is_file()
    ) == [RUN_MANIFEST_FILENAME]

    # The status alone is enough afterwards: a checkpoint written later must not
    # make a run whose private inputs were deleted look resumable.
    swept = load_manifest(run_id, root=workspace.runs_root)
    assert swept.status is RunStatus.EXPIRED
    write_checkpoint(swept, root=workspace.runs_root, now=expired_moment)
    assert (
        describe_resume(swept, swept.fingerprint, now=expired_moment).decision
        is ResumeDecision.EXPIRED
    )


def test_the_thirty_day_boundary_keeps_a_run_resumable(workspace: Workspace) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, run_id, StepId.C_AUTHORED, at=_NOW)
    boundary = _NOW + timedelta(days=30)

    assert expire_stale_runs(workspace.runs_root, now=boundary) == []
    assert _step(workspace, run_id, StepId.C_AUTHORED).is_dir()

    resumed = _resume(workspace, run_id, manifest.fingerprint, at=boundary)
    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.steps_run[0] is StepId.D_MISTAKES


def test_a_completed_run_is_never_offered_for_resume(workspace: Workspace) -> None:
    manifest = _start(workspace)
    manifest = _advance_through(workspace, manifest.run_id, StepId.E_VERIFIED, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)

    assessment = describe_resume(manifest, manifest.fingerprint, now=_LATER)

    assert assessment.decision is ResumeDecision.EXPIRED
    assert list_unfinished(workspace.runs_root, now=_LATER) == []


# -- retention -------------------------------------------------------------


def _failing_extract(
    self: ClaudeCodeAdapter, instance: SourceInstanceRecord, snapshot_dir: Path
) -> Iterator[NormalizedUtterance]:
    msg = "extraction failed after the snapshot was taken"
    raise RuntimeError(msg)


def test_expiry_deletes_snapshots_left_by_a_failed_extraction(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest-bounded snapshot cleanup never runs here, so retention must."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    monkeypatch.setattr(ClaudeCodeAdapter, "extract", _failing_extract)
    result = collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    assert result["excluded_instances"], "the failure must be reported, not hidden"

    snapshots = _run_directory(workspace, run_id) / "snapshots"
    copied = [path for path in snapshots.rglob("*") if path.is_file()]
    assert copied, "an interrupted extraction leaves the copied source behind"
    assert any(b"learning progress" in path.read_bytes() for path in copied)

    stale = load_manifest(run_id, root=workspace.runs_root)
    write_checkpoint(stale, root=workspace.runs_root, now=_NOW - timedelta(days=31))
    assert expire_stale_runs(workspace.runs_root, now=_NOW) == [run_id]
    assert not snapshots.exists()


def test_completed_cleanup_keeps_only_the_package_and_the_manifest(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    run_directory = _run_directory(workspace, run_id)
    manifest = _advance_through(workspace, run_id, StepId.E_VERIFIED, at=_NOW)
    log_event(run_directory, "checkpoint_written", step_id=StepId.E_VERIFIED)
    (run_directory / "submission" / "package.json").write_text("{}", encoding="utf-8")
    # A source whose extraction failed can leave a snapshot behind; completion
    # must not keep it either.
    leftover = run_directory / "snapshots" / "claude_code" / "abc"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "sessions.jsonl").write_text("I very like this plan\n", encoding="utf-8")
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)

    cleanup_completed(run_directory)

    survivors = sorted(
        str(path.relative_to(run_directory)) for path in run_directory.rglob("*") if path.is_file()
    )
    assert survivors == [RUN_MANIFEST_FILENAME, "submission/package.json"]
    for path in run_directory.rglob("*"):
        if path.is_file():
            assert b"I very like" not in path.read_bytes()
    assert load_manifest(run_id, root=workspace.runs_root).status is RunStatus.COMPLETED


# -- cleanup can never leave the run directory -----------------------------


def _source_home(tmp_path: Path) -> Path:
    """A stand-in for the user's own application data and home directory."""
    home = tmp_path / "victim-home"
    projects = home / ".claude" / "projects" / "-home-tester-notes"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text("private source text\n", encoding="utf-8")
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.txt").write_text("keep", encoding="utf-8")
    return home


def _victim_files(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def test_cleanup_refuses_a_run_directory_that_is_a_symlink(
    workspace: Workspace, tmp_path: Path
) -> None:
    # The worst case for a store that deletes fixed names under a directory: a
    # link named like a run, holding a manifest that matches that name. Every
    # containment check resolves the run directory first, so without a check on
    # the link itself the target's own 'steps' and 'logs' are inside the bounds
    # and a home directory is one deletion away.
    home = _source_home(tmp_path)
    planted_id = "run-" + "a" * 32
    for name in ("steps", "logs", "snapshots", "submission"):
        (home / name).mkdir()
        (home / name / "keep.txt").write_text("keep", encoding="utf-8")
    real = _start(workspace)
    completed = load_manifest(real.run_id, root=workspace.runs_root).model_copy(
        update={"run_id": planted_id, "status": RunStatus.COMPLETED}
    )
    write_model(home / RUN_MANIFEST_FILENAME, completed)
    before = _victim_files(home)
    planted = workspace.runs_root / planted_id
    planted.symlink_to(home, target_is_directory=True)

    with pytest.raises(RunStoreError, match="symlink") as excinfo:
        cleanup_completed(planted)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_UNSAFE_CLEANUP_PATH"

    # The same link, now claiming an unfinished run far past the retention
    # limit, is what the launch-time sweep walks over.
    stale = completed.model_copy(
        update={"status": RunStatus.CHECKPOINTED, "last_checkpoint_at": _NOW - timedelta(days=400)}
    )
    write_model(home / RUN_MANIFEST_FILENAME, stale)
    assert expire_stale_runs(workspace.runs_root, now=_NOW) == []
    assert [summary.run_id for summary in list_unfinished(workspace.runs_root, now=_NOW)] == [
        real.run_id
    ]
    assert _victim_files(home) == before | {
        RUN_MANIFEST_FILENAME: (home / RUN_MANIFEST_FILENAME).read_bytes()
    }


def test_cleanup_refuses_a_step_directory_pointing_at_source_data(
    workspace: Workspace, tmp_path: Path
) -> None:
    home = _source_home(tmp_path)
    before = _victim_files(home)
    manifest = _start(workspace)
    manifest = _advance_through(workspace, manifest.run_id, StepId.E_VERIFIED, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)
    run_directory = _run_directory(workspace, manifest.run_id)
    # 'steps' is where every step's artifacts live now, so it is the subtree
    # completed cleanup deletes and therefore the one a link could redirect.
    shutil.rmtree(run_directory / "steps")
    (run_directory / "steps").symlink_to(home / ".claude", target_is_directory=True)

    with pytest.raises(RunStoreError, match="symlink") as excinfo:
        cleanup_completed(run_directory)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_UNSAFE_CLEANUP_PATH"
    assert _victim_files(home) == before


def test_cleanup_refuses_a_directory_that_is_not_a_run(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _victim_files(home)

    with pytest.raises(RunStoreError) as excinfo:
        cleanup_completed(home)

    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_CHECKPOINT_CORRUPT"
    assert _victim_files(home) == before


def test_retention_touches_nothing_outside_the_run_directory(
    workspace: Workspace, tmp_path: Path
) -> None:
    home = _source_home(tmp_path)
    victim_before = _victim_files(home)
    stale = _start(workspace)
    _advance_through(workspace, stale.run_id, StepId.C_AUTHORED, at=_NOW - timedelta(days=31))
    live = _start(workspace)
    _advance_through(workspace, live.run_id, StepId.A_COLLECTED, at=_NOW)
    neighbor = workspace.runs_root.parent / "calibration"
    neighbor.mkdir(parents=True, exist_ok=True)
    (neighbor / "local-history.jsonl").write_text("{}\n", encoding="utf-8")
    loose = workspace.runs_root / "not-a-run.txt"
    loose.write_text("keep", encoding="utf-8")

    assert expire_stale_runs(workspace.runs_root, now=_NOW) == [stale.run_id]

    assert _victim_files(home) == victim_before
    assert (neighbor / "local-history.jsonl").is_file()
    assert loose.is_file()
    assert (workspace.repo / ".gitignore").is_file()
    assert _step(workspace, live.run_id, StepId.A_COLLECTED).is_dir()
    assert not (_run_directory(workspace, stale.run_id) / "steps").exists()
