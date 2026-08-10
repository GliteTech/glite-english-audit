"""The whole five-step run, on disk, through the drivers an agent invokes.

One session is one file, and that file keeps its name from step a to step e.
Every claim this project makes about a run — how many words were counted, which
sessions were judged, what was shared and what was withheld — is a claim about
those files. So this test runs the real drivers over the committed synthetic
fixtures on a temporary runs root and then reads the run back off disk, rather
than reproducing the transformations in memory where the file layout cannot be
wrong.

Only the agents are faked. Each of steps c, d and e is handed a projection and
answers with a decision its driver expands into the session file, so the fakes
here write exactly what each step asks for: a deterministic authorship judgment,
a deterministic pair of mistake drafts, and a second reader that names the one
record it will not share. No fake writes a session file, which is why a
bookkeeping field being right on disk is evidence about the drivers rather than
about this module. Everything between the files is the product's own code, and
no model and no network are involved anywhere.

The fixture home is copied and given one extra message: a sentence dictated in
Wispr Flow, pasted into Claude Code two minutes later. That is the case
deduplication exists for, and the two copies land in different session files by
construction, so nothing that reads one file at a time could ever collapse them.
"""

import json
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest

from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.adapters.wispr_flow import create_adapter as wispr_flow_adapter
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    Modality,
    OsEnvironment,
    StepId,
)
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import (
    MistakeRecord,
    ReviewedSubmissionArtifact,
)
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.paths import step_dir, submission_dir
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
    DECISION_SUFFIX,
    PROJECTION_SUFFIX,
    VERDICT_SUFFIX,
    AuthoredLine,
    DropList,
    MistakeDraft,
    RecordForConfidentiality,
    UtteranceForJudgment,
)
from glite_english_audit.pipeline.deduplicate import REMOVED_NAME
from glite_english_audit.sessions import (
    INDEX_NAME,
    read_all,
    read_index,
    read_session,
    session_files,
)
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

# Dictated in Wispr Flow at 10:00 and pasted into Claude Code at 10:02.
_DICTATED = "I very like this plan, let us start from the first step."

# What the fake step-c agent treats as someone else's words, and what the fake
# step-d agent flags. Both are fixed rules rather than judgments, so the run is
# reproducible; which sentences they hit is a property of the fixtures.
_PASTED_MARKER = "deploy script"
_FLAGGED = (
    (
        "very like",
        "Used 'very' to modify the verb 'like'.",
        "In English, 'very' cannot modify a verb; use 'really' instead.",
        "I really like this plan.",
    ),
    (
        "I am agree",
        "Used 'I am agree' where English needs 'I agree'.",
        "'Agree' is a verb in English, so it takes no form of 'to be'.",
        "I agree with the second option.",
    ),
)


class _AuditedRun(NamedTuple):
    """One finished run on disk, with what each driver reported about it."""

    run_id: str
    runs_root: Path
    collected: dict[str, object]
    deduplicated: dict[str, object]
    prepared_c: authorship.PreparedStep
    authored: authorship.AuthorshipApplication
    assigned_d: list[mistakes.SessionAssignment]
    produced: mistakes.MistakesOutcome
    withheld: MistakeRecord
    verified: verify.VerificationOutcome
    reviewed: ReviewedSubmissionArtifact

    def step(self, step: StepId) -> Path:
        return step_dir(self.run_id, step, root=self.runs_root)


def _checkout_with_an_ignored_runtime(tmp_path: Path) -> Path:
    """A minimal repository whose runtime tree Git ignores.

    Snapshots are copies of the user's own application data, so the snapshot
    gate asks Git — not a convention — whether the target is ignored before a
    byte is copied. Without a real repository, collect refuses to run.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=checkout,
        check=True,
    )
    return checkout


def _home_with_a_pasted_dictation(tmp_path: Path) -> Path:
    """The Claude Code fixture home, plus one session holding a paste.

    The message repeats a Wispr Flow dictation word for word. It is its own
    session in its own project, which is what makes the pair unreachable to any
    pass that sees one file at a time.
    """
    home = tmp_path / "home-claude-code"
    shutil.copytree(_FIXTURES / "claude_code" / "success" / "home", home)
    session_id = "33333333-3333-4333-8333-333333333333"
    project = home / ".claude" / "projects" / "-home-tester-repos-api"
    project.mkdir(parents=True)
    (project / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "uuid": "p1",
                "parentUuid": None,
                "timestamp": "2026-06-01T10:02:00Z",
                "sessionId": session_id,
                "cwd": "/home/tester/repos/api",
                "version": "2.1.210",
                "gitBranch": "main",
                "userType": "external",
                "entrypoint": "cli",
                "isSidechain": False,
                "type": "user",
                "message": {"role": "user", "content": _DICTATED},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return home


def _started_run(tmp_path: Path, runs_root: Path) -> str:
    """Discovery over two fixture homes, then a run with both consents."""
    records = []
    instance_paths: dict[str, str] = {}
    for adapter, home in (
        (claude_code_adapter(), _home_with_a_pasted_dictation(tmp_path)),
        (wispr_flow_adapter(), _FIXTURES / "wispr_flow" / "success" / "home"),
    ):
        outcome = adapter.discover(
            DiscoveryContext(os_environment=OsEnvironment.MACOS, home=home, now=_NOW, environ={})
        )
        records.extend(outcome.records)
        instance_paths.update({key: str(path) for key, path in outcome.instance_paths.items()})
    assert records, "the fixtures must discover at least one instance"

    inventory_dir = ensure_private_dir(tmp_path / "inventory")
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(records=records, instance_paths=instance_paths, created_at=_NOW),
    )
    # An audit defaults to Claude Code alone. These end-to-end cases exercise
    # what happens *across* applications -- a dictation pasted into an editor,
    # counted once -- so they ask for every discovered source by name, which is
    # the same one flag a learner uses when Claude Code is not enough.
    every_app = sorted({record.adapter_id for record in records})
    manifest = start_run.start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=runs_root,
        inventory_dir=inventory_dir,
        include_sources=every_app,
        local_scan_consent=True,
        provider_transfer_consent=True,
        now=_NOW,
    )
    return manifest.run_id


def _authored_text(text: str) -> str:
    """One agent's authorship judgment, by rule instead of by model.

    The rules are arbitrary and fixed: anything about the deploy script was
    pasted, a message carrying markup keeps only what follows it, and the rest
    is the learner's own. Both reductions have to be visible on disk — an
    entirely pasted message keeps its place with empty text, and a partial one
    keeps a verbatim span — which is what the step-c verifier checks.
    """
    if _PASTED_MARKER in text:
        return ""
    if ">" in text:
        return text[text.rindex(">") + 1 :].strip()
    return text


def _write_step_c(prepared: authorship.PreparedStep) -> None:
    """Answer every projection with one decision per item it was given.

    Index and kept text, and nothing else: the session file is the driver's to
    write, so a bookkeeping field is no longer something this fake could get
    wrong and no longer something the run has to take on trust.
    """
    for session in prepared.sessions:
        projected = read_jsonl_models(Path(session.input_path), UtteranceForJudgment)
        write_jsonl_models(
            Path(session.output_path),
            [AuthoredLine(i=item.i, text=_authored_text(item.text)) for item in projected],
        )


def _drafts_for(utterances: Iterable[UtteranceForJudgment]) -> list[MistakeDraft]:
    """The step-d agent: one draft per flagged phrase, addressed by index.

    What it judged and where, and nothing else. The utterance ID, the source and
    the modality it used to copy are the driver's to re-derive from the line the
    index names, so a record on disk carrying the wrong one of the three is now
    the expander's fault and never this fake's.
    """
    drafts: list[MistakeDraft] = []
    for utterance in utterances:
        for phrase, mistake, rule, example in _FLAGGED:
            start = utterance.text.find(phrase)
            if start < 0:
                continue
            drafts.append(
                MistakeDraft(
                    i=utterance.i,
                    span=(start, start + len(phrase)),
                    mistake=mistake,
                    rule=rule,
                    example=example,
                    example_type=ExampleType.SYNTHETIC,
                )
            )
    return drafts


def _lines(path: Path) -> int:
    """Non-blank lines in a file, counted without the drivers' own readers."""
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


@pytest.fixture(scope="module")
def audited(tmp_path_factory: pytest.TempPathFactory) -> _AuditedRun:
    """One complete run, built once and then read back off disk by every test."""
    tmp_path = tmp_path_factory.mktemp("e2e")
    runs_root = tmp_path / "runs"
    checkout = _checkout_with_an_ignored_runtime(tmp_path)
    run_id = _started_run(tmp_path, runs_root)

    collected = collect.collect(run_id, runs_root=runs_root, repo=checkout)
    deduplicated = deduplicate.deduplicate(run_id, runs_root=runs_root)

    prepared_c = authorship.prepare(run_id, runs_root=runs_root)
    _write_step_c(prepared_c)
    authored = authorship.apply_authorship(run_id, runs_root=runs_root)

    assigned_d = mistakes.prepare_mistakes(run_id, runs_root=runs_root)
    for assignment in assigned_d:
        projected = read_jsonl_models(Path(assignment.read), UtteranceForJudgment)
        write_jsonl_models(Path(assignment.write), _drafts_for(projected))
    produced = mistakes.apply_mistakes(run_id, runs_root=runs_root)

    # The second reader withholds the last record it was shown and confirms the
    # rest. It answers with indices into the file it read, so removal is the only
    # disagreement it can express at all, and the counts must show this one.
    withholding = [entry for entry in produced.next_step if entry.items][-1]
    withheld = list(
        read_jsonl_models(
            step_dir(run_id, StepId.D_MISTAKES, root=runs_root) / withholding.name, MistakeRecord
        )
    )[-1]
    for assignment in produced.next_step:
        shown = list(read_jsonl_models(Path(assignment.read), RecordForConfidentiality))
        drop = [shown[-1].i] if assignment.name == withholding.name else []
        write_model(Path(assignment.write), DropList(drop=drop))
    verified = verify.apply_verification(run_id, runs_root=runs_root)

    reviewed = build_review.build_review(run_id, runs_root=runs_root)
    return _AuditedRun(
        run_id=run_id,
        runs_root=runs_root,
        collected=collected,
        deduplicated=deduplicated,
        prepared_c=prepared_c,
        authored=authored,
        assigned_d=assigned_d,
        produced=produced,
        withheld=withheld,
        verified=verified,
        reviewed=reviewed,
    )


def test_no_step_reported_a_problem_with_the_files_it_was_given(audited: _AuditedRun) -> None:
    # The steps below are read as evidence about the layout, so a run that
    # limped through with a quarantined session would make every count that
    # follows describe less than the whole fixture.
    assert audited.collected["excluded_instances"] == []
    assert audited.authored.diagnostics == []
    assert audited.authored.sessions_quarantined == 0
    assert audited.produced.passed
    assert audited.verified.passed


def test_every_step_holds_the_same_session_files(audited: _AuditedRun) -> None:
    """The property the whole pipeline shape exists to provide.

    A step that skipped a file it could not process would leave a run that
    looks finished and counts less text than the person selected.
    """
    names = [path.name for path in session_files(audited.step(StepId.A_COLLECTED))]
    assert len(names) == audited.collected["sessions"]
    assert len(names) > 1, "one session file would make the invariant vacuous"
    for step in StepId:
        assert [path.name for path in session_files(audited.step(step))] == names


def test_a_session_that_produced_nothing_is_an_empty_file_not_a_missing_one(
    audited: _AuditedRun,
) -> None:
    """Missing and empty mean different things, and only one of them happened.

    Most sessions contain no mistake at all. Each of those still owes step d
    and step e a file, because a run cannot otherwise tell "this session was
    read and had nothing" from "this session was never read".
    """
    empty = [
        path.name
        for path in session_files(audited.step(StepId.D_MISTAKES))
        if path.stat().st_size == 0
    ]
    assert empty, "the fixtures must include a session with no mistakes"
    assert audited.produced.sessions - audited.produced.sessions_with_records == len(empty)
    for name in empty:
        for step in (StepId.D_MISTAKES, StepId.E_VERIFIED):
            path = audited.step(step) / name
            assert path.is_file()
            assert path.read_text(encoding="utf-8") == ""


def test_a_dictation_pasted_into_another_application_is_counted_once(
    audited: _AuditedRun,
) -> None:
    """Deduplication is global, and only a global pass could have caught this.

    The dictation and the paste have different session identifiers by
    construction, so they are in different files. A per-file pass would keep
    both, and both would land in the denominator of every rate this run
    reports.
    """
    origins = {
        path.name: {
            utterance.source_adapter for utterance in members if utterance.text == _DICTATED
        }
        for path, members in read_all(audited.step(StepId.A_COLLECTED))
    }
    pasted = [name for name, adapters in origins.items() if "claude_code" in adapters]
    dictated = [name for name, adapters in origins.items() if "wispr_flow" in adapters]
    assert len(pasted) == 1 and len(dictated) == 1
    assert pasted != dictated, "the two copies must start in different session files"

    removed = json.loads(
        (audited.step(StepId.B_DEDUPLICATED) / REMOVED_NAME).read_text(encoding="utf-8")
    )["removed"]
    assert list(removed) == pasted
    assert audited.deduplicated["removed"] == 1

    # The typed copy goes and the dictation stays: it is the original event.
    survivors = {
        path.name: [utterance.text for utterance in members]
        for path, members in read_all(audited.step(StepId.B_DEDUPLICATED))
    }
    assert _DICTATED in survivors[dictated[0]]
    assert survivors[pasted[0]] == []
    assert (audited.step(StepId.B_DEDUPLICATED) / pasted[0]).stat().st_size == 0


def test_step_c_returns_every_item_it_was_given(audited: _AuditedRun) -> None:
    """An utterance the learner did not write comes back empty, not deleted.

    Keeping the item is what lets step c's file be diffed against step b's line
    by line, and it is the difference between "the learner wrote none of this"
    and "this message was lost somewhere in the run".
    """
    for path, members in read_all(audited.step(StepId.C_AUTHORED)):
        assert len(members) == len(read_session(audited.step(StepId.B_DEDUPLICATED) / path.name))
    emptied = [
        utterance
        for _, members in read_all(audited.step(StepId.C_AUTHORED))
        for utterance in members
        if not utterance.text.strip()
    ]
    assert emptied, "the fake judgment must empty at least one utterance"
    for utterance in emptied:
        original = next(
            item
            for _, members in read_all(audited.step(StepId.B_DEDUPLICATED))
            for item in members
            if item.utterance_id == utterance.utterance_id
        )
        assert _PASTED_MARKER in original.text


def test_the_word_denominator_is_what_the_step_c_files_hold(audited: _AuditedRun) -> None:
    """The denominator of every rate this product reports, counted from disk.

    Two counters answer this question — the step-c index counts the English
    slice of each file, the review counts the words it will publish a rate
    over — and a third count taken here, straight off the files, has to agree
    with both. The fixture text is English throughout, so the English slice is
    the whole text and the three numbers are the same number.
    """
    kept = [
        utterance
        for _, members in read_all(audited.step(StepId.C_AUTHORED))
        for utterance in members
        if utterance.text.strip()
    ]
    direct = sum(count_words(utterance.text) for utterance in kept)
    assert direct > 0
    assert audited.authored.index.word_count == direct
    assert audited.authored.words_after == direct
    assert audited.reviewed.counts.eligible_english_words == direct
    assert audited.reviewed.counts.eligible_utterances == len(kept)

    # Strictly smaller than what step b held: the duplicate went first, and the
    # pasted material went with the authorship judgment. A denominator that
    # matched step b would mean neither reduction reached the count.
    before = sum(
        count_words(utterance.text)
        for _, members in read_all(audited.step(StepId.B_DEDUPLICATED))
        for utterance in members
    )
    assert 0 < direct < before


def test_the_published_counts_reconcile_with_the_records_on_disk(audited: _AuditedRun) -> None:
    """Every count in the review is a claim about a file in this run.

    Step d writes what was found and step e may only take records away, so the
    verified total is the step-d lines, what is shared is the step-e lines, and
    the difference is what was withheld. Nothing else can explain a gap.
    """
    counts = audited.reviewed.counts
    in_d = sum(_lines(path) for path in session_files(audited.step(StepId.D_MISTAKES)))
    in_e = sum(_lines(path) for path in session_files(audited.step(StepId.E_VERIFIED)))
    assert in_d > in_e > 0, "the run must both share and withhold something"
    assert counts.verified_total_mistakes == in_d
    assert counts.shared_mistakes == in_e == len(audited.reviewed.records)
    assert counts.withheld_for_privacy == in_d - in_e
    assert counts.withheld_by_user == 0
    assert counts.other_withheld == {}
    assert (
        counts.shared_mistakes + counts.withheld_by_user + counts.withheld_for_privacy
        == counts.verified_total_mistakes
    )

    # Both modalities are present in this run, and the two must add up to the
    # whole: a modality split that loses words is a rate reported per modality
    # that no longer describes the same corpus.
    assert counts.written.eligible_words > 0
    assert counts.spoken_asr.eligible_words > 0
    assert (
        counts.written.eligible_words + counts.spoken_asr.eligible_words
        == counts.eligible_english_words
    )
    assert (
        counts.written.eligible_utterances + counts.spoken_asr.eligible_utterances
        == counts.eligible_utterances
    )


def test_the_record_step_e_withheld_is_not_in_the_review(audited: _AuditedRun) -> None:
    shared = {record.mistake_id for record in audited.reviewed.records}
    assert audited.withheld.record_id not in shared
    assert audited.verified.records_dropped == 1
    assert audited.verified.records_kept == len(shared)

    dropped = json.loads(
        (audited.step(StepId.E_VERIFIED) / verify.DROPPED_NAME).read_text(encoding="utf-8")
    )["dropped"]
    assert [record for names in dropped.values() for record in names] == [
        audited.withheld.record_id
    ]


def test_the_session_index_travels_with_the_files_and_reaches_no_agent(
    audited: _AuditedRun,
) -> None:
    """File names are opaque; the mapping to a session stays on the machine.

    A filename reaches a model's context, so the sequence number is all it may
    carry. The index is what makes those numbers mean something locally, and it
    has to be in every step directory for the run to stay readable — while
    appearing in nothing a step hands an agent to read.
    """
    index = read_index(audited.step(StepId.A_COLLECTED))
    assert index, "step a must record which sequence number belongs to which session"
    names = {path.name for path in session_files(audited.step(StepId.A_COLLECTED))}
    assert set(index) == names
    for step in StepId:
        directory = audited.step(step)
        assert (directory / INDEX_NAME).is_file()
        assert read_index(directory) == index
        # The index sits beside the session files and is never one of them, so
        # no step can hand it out as work.
        assert INDEX_NAME not in {path.name for path in session_files(directory)}

    handed_out = json.dumps(
        {
            "step_c": audited.prepared_c.model_dump(mode="json"),
            "step_d": [assignment.model_dump(mode="json") for assignment in audited.assigned_d],
            "step_e": [
                assignment.model_dump(mode="json") for assignment in audited.produced.next_step
            ],
        }
    )
    assert INDEX_NAME not in handed_out
    for session_hash in index.values():
        assert session_hash not in handed_out
    # And every file named for an agent belongs to a session of this run. Each
    # step hands over a projection and takes back a decision rather than the
    # session file itself, but both are that file's name plus a fixed suffix, so
    # what a name carries a model is still the sequence number and nothing else.
    for_agents = names | {
        name.removesuffix(".jsonl") + suffix
        for name in names
        for suffix in (PROJECTION_SUFFIX, DECISION_SUFFIX, VERDICT_SUFFIX)
    }
    named = [Path(entry.input_path).name for entry in audited.prepared_c.sessions]
    named += [Path(entry.output_path).name for entry in audited.prepared_c.sessions]
    named += [Path(entry.read).name for entry in audited.assigned_d]
    named += [Path(entry.write).name for entry in audited.assigned_d]
    named += [Path(entry.read).name for entry in audited.produced.next_step]
    named += [Path(entry.write).name for entry in audited.produced.next_step]
    assert set(named) <= for_agents


def test_the_run_ends_in_a_package_that_passes_the_full_gate(audited: _AuditedRun) -> None:
    stored = read_model(
        submission_dir(audited.run_id, root=audited.runs_root) / build_review.REVIEWED_NAME,
        ReviewedSubmissionArtifact,
    )
    assert stored.counts == audited.reviewed.counts
    assert [record.mistake_id for record in stored.records] == [
        record.mistake_id for record in audited.reviewed.records
    ]
    assert all(record.included for record in stored.records)

    package = materialize_package(stored)
    assert verify_submission_package(package) == []
    assert verify_package_against_review(package, stored) == []
    assert len(package.records) == len(stored.records)
    # Nothing that could name a session, a file, or a machine goes with it.
    exported = package.model_dump_json()
    for session_hash in read_index(audited.step(StepId.E_VERIFIED)).values():
        assert session_hash not in exported
    assert "session-" not in exported
    assert package.model_validate_json(exported) == package


def test_every_shared_record_still_addresses_the_text_it_quotes(audited: _AuditedRun) -> None:
    """The span is the only link between a record and the learner's words.

    Records carry no quoted text: what the learner wrote is resolved from the
    step-c file through the span, which is what makes an invented quote
    impossible rather than merely detectable. A run whose spans no longer land
    inside their utterances would publish mistakes about nothing.
    """
    texts = {
        utterance.utterance_id: utterance.text
        for _, members in read_all(audited.step(StepId.C_AUTHORED))
        for utterance in members
    }
    quoted: list[str] = []
    for path in session_files(audited.step(StepId.E_VERIFIED)):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = MistakeRecord.model_validate_json(line)
            text = texts[record.utterance_id]
            span = record.evidence_span
            assert span.end <= len(text)
            quoted.append(text[span.start : span.end])
    assert quoted, "the run must share at least one record"
    assert all(phrase in {entry[0] for entry in _FLAGGED} for phrase in quoted)
    # A record reports the modality of the utterance it addresses, so a run over
    # a typed session and a dictated one publishes both. Reading it off the
    # record's own source would have made every mistake in this run written.
    assert {record.record.modality for record in audited.reviewed.records} == {
        Modality.WRITTEN,
        Modality.SPOKEN_ASR,
    }
