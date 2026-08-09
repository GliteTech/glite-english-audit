"""Steps d and e: records that are clean when written, then a reader that may only remove.

These two steps decide the number the product publishes. Step d's records are
the numerator of the learner's error rate, so every way that numerator can be
wrong belongs here: a span that addresses text nobody wrote, a draft addressing
a line this session does not hold, and two records billing the same characters
twice.

The rule these tests exist to hold is that step d fails a file rather than
repairing it. A dropped record leaves a smaller count with nothing to explain
it, which turns a defect in the producing skill into a published number and
makes step d's own failure rate unmeasurable.

Step e is the second reader, and the reason a run with step e removed would
still be right is that step e may only delete. It used to be asked for the
records it confirmed, so adding, altering, repeating and reordering were four
ways it could become a second author, and each had to arrive as its own
diagnostic. It is now asked for the indices to drop and the driver rebuilds the
file from step d's own records, so all four are unrepresentable and the checks
that caught them are gone from ``pipeline/verify.py``. What is left is what a
list of indices can get wrong: an index past the end, an index named twice, and
a verdict that never arrived or does not parse.

Neither agent reads or writes a step file any more. Each is handed a projection
and returns a decision, so what these tests fake is the decision; the file on
disk is the driver's answer to it.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    Modality,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
    TextStatus,
)
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_step_map,
)
from glite_english_audit.artifacts.models import EvidenceSpan, MistakeRecord, NormalizedUtterance
from glite_english_audit.consent import CONSENT_POLICY_VERSION, MissingConsentError
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import repo_root, step_dir
from glite_english_audit.pipeline import mistakes, verify
from glite_english_audit.pipeline.agent_io import (
    DropList,
    MistakeDraft,
    RecordForConfidentiality,
    UtteranceForJudgment,
    decision_path,
    projection_path,
    verdict_path,
)
from glite_english_audit.pipeline.record_step import advance_to
from glite_english_audit.sessions import write_index
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME, load_manifest

_RUN = "run-" + "4" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

_FIRST = "session-0001.jsonl"
_SECOND = "session-0002.jsonl"

#                     0    5    10   15   20   25   30   35   40   45   50
#                     |    |    |    |    |    |    |    |    |    |    |
_SESSION_ONE = "I have went to the store and buyed a bread yesterday."
_WENT = (7, 11)
_A_BREAD = (35, 42)
_SESSION_TWO = "He explained me the plan."


def _utterance(index: int, text: str) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"claude_code:u{index}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash=str(index) * 64,
        timestamp=datetime(2026, 8, 1, 12, index, tzinfo=UTC),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="c" * 64,
    )


def _draft(
    index: int,
    span: tuple[int, int],
    *,
    rule: str = "Use the article before a singular countable noun.",
    mistake: str = "The article is missing before a countable noun.",
) -> MistakeDraft:
    """What a step-d agent decides: a line of its projection, and where in it.

    ``index`` is one-based and local to the session file, so it is the same
    number for session two's only utterance as for session one's first. The
    utterance ID, source type and modality the record ends up carrying are the
    driver's to re-derive.
    """
    return MistakeDraft(
        i=index,
        span=span,
        mistake=mistake,
        rule=rule,
        example="I opened the door and went outside.",
        example_type=ExampleType.SYNTHETIC,
    )


def _record(
    index: int,
    span: tuple[int, int],
    *,
    rule: str = "Use the article before a singular countable noun.",
    mistake: str = "The article is missing before a countable noun.",
) -> MistakeRecord:
    """The artifact a draft becomes, named by the utterance rather than the line."""
    return MistakeRecord(
        utterance_id=f"claude_code:u{index}",
        evidence_span=EvidenceSpan(start=span[0], end=span[1]),
        mistake=mistake,
        rule=rule,
        example="I opened the door and went outside.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


# One step-d record serialized the way step e used to answer: a plausible thing
# for an agent following an older skill to write where a drop list belongs.
_OLD_SHAPE = _record(1, _WENT).model_dump_json() + "\n"


def _seed(runs_root: Path, *, provider_transfer_consent: bool = True) -> None:
    """A run whose steps a, b and c are promoted and whose step-c files exist.

    Two sessions, because the checks that matter most are about one session's
    file not borrowing from another's.
    """
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=_NOW,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.AWAITING_PREFLIGHT,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=_NOW,
            provider_transfer_confirmed_at=_NOW if provider_transfer_consent else None,
        ),
        steps=empty_step_map(),
        fingerprint=CompatibilityFingerprint(
            adapter_versions={},
            artifact_schema_version=MANIFEST_SCHEMA_VERSION,
            tokenizer_version=TOKENIZER_VERSION,
            skill_versions={},
            prompt_versions={},
            model_ids={},
            consent_policy_version=CONSENT_POLICY_VERSION,
        ),
    )
    write_model(ensure_private_dir(runs_root / _RUN) / RUN_MANIFEST_FILENAME, manifest)
    for step in (StepId.A_COLLECTED, StepId.B_DEDUPLICATED, StepId.C_AUTHORED):
        advance_to(_RUN, step, StepStatus.PROMOTED, producer_version="0.1.0", runs_root=runs_root)

    corpus = ensure_private_dir(step_dir(_RUN, StepId.C_AUTHORED, root=runs_root))
    write_jsonl_models(
        corpus / _FIRST,
        # The empty second utterance is what step c leaves behind when every
        # word of a message turned out to be someone else's.
        [_utterance(1, _SESSION_ONE), _utterance(2, "")],
    )
    write_jsonl_models(corpus / _SECOND, [_utterance(3, _SESSION_TWO)])
    write_index(corpus, {_FIRST: "1" * 64, _SECOND: "3" * 64})


def _write_drafts(runs_root: Path, name: str, drafts: list[MistakeDraft]) -> Path:
    """Stand in for the step-d agent: it decides, and the driver writes the file."""
    path = decision_path(step_dir(_RUN, StepId.D_MISTAKES, root=runs_root), name)
    write_jsonl_models(path, drafts)
    return path


def _write_verdict(runs_root: Path, name: str, drop: list[int]) -> Path:
    """Stand in for the step-e agent: one object for the file, not one per record."""
    path = verdict_path(step_dir(_RUN, StepId.E_VERIFIED, root=runs_root), name)
    write_model(path, DropList(drop=drop))
    return path


def _write_artifact(runs_root: Path, step: StepId, name: str, records: list[MistakeRecord]) -> Path:
    """Put a step file on disk directly, which now only the driver ever does."""
    path = step_dir(_RUN, step, root=runs_root) / name
    write_jsonl_models(path, records)
    return path


def _codes(diagnostics: list[Diagnostic]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _on_disk(runs_root: Path, step: StepId, name: str) -> list[MistakeRecord]:
    """What one step's file actually holds, read the way the next step reads it."""
    records, diagnostics = mistakes.read_records(step_dir(_RUN, step, root=runs_root) / name)
    assert diagnostics == []
    return records


def _lines(runs_root: Path, step: StepId, name: str) -> list[str]:
    """One step file's own lines, so a copy can be checked as a copy."""
    path = step_dir(_RUN, step, root=runs_root) / name
    return path.read_text(encoding="utf-8").splitlines()


def _status(runs_root: Path, step: StepId) -> StepStatus:
    return load_manifest(_RUN, root=runs_root).steps[step].status


def _promote_step_d(runs_root: Path, first: list[MistakeDraft]) -> None:
    """Reach the state step e starts from: a promoted step d holding ``first``."""
    _seed(runs_root)
    mistakes.prepare_mistakes(_RUN, runs_root=runs_root)
    _write_drafts(runs_root, _FIRST, first)
    _write_drafts(runs_root, _SECOND, [])
    outcome = mistakes.apply_mistakes(_RUN, runs_root=runs_root)
    assert outcome.passed, _codes(outcome.diagnostics)


def _dropped(runs_root: Path) -> dict[str, list[str]]:
    path = step_dir(_RUN, StepId.E_VERIFIED, root=runs_root) / verify.DROPPED_NAME
    payload: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))["dropped"]
    return payload


def _dropped_exists(runs_root: Path) -> bool:
    return (step_dir(_RUN, StepId.E_VERIFIED, root=runs_root) / verify.DROPPED_NAME).is_file()


# --- step d: the records must be clean when they are written ---------------


def test_the_fixture_spans_address_the_words_the_tests_name() -> None:
    # Every step-d case below asserts something about a span, so the spans
    # themselves must not be the thing that is wrong.
    assert _SESSION_ONE[slice(*_WENT)] == "went"
    assert _SESSION_ONE[slice(*_A_BREAD)] == "a bread"


def test_preparing_step_d_names_one_file_per_session_and_counts_only_written_text(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    assignments = mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)

    target = step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path)
    assert [assignment.name for assignment in assignments] == [_FIRST, _SECOND]
    # The agent is sent to a projection and asked for a decision, both under the
    # step's own agent directory. It never opens the step-c file and never
    # writes the step-d artifact, so neither can be swapped for the other.
    assert [Path(a.read) for a in assignments] == [
        projection_path(target, _FIRST),
        projection_path(target, _SECOND),
    ]
    assert [Path(a.write) for a in assignments] == [
        decision_path(target, _FIRST),
        decision_path(target, _SECOND),
    ]
    # Session one holds two utterances and one of them is empty: an agent sent
    # to read two items in that file would spend a turn on nothing.
    assert [assignment.items for assignment in assignments] == [1, 1]
    assert [assignment.words for assignment in assignments] == [
        count_words(_SESSION_ONE),
        count_words(_SESSION_TWO),
    ]


def test_the_step_d_projection_numbers_the_utterance_step_c_emptied_too(tmp_path: Path) -> None:
    # `items` counts what is worth reading; `i` addresses a line. Skipping the
    # emptied utterance would shift every index after it, and the driver would
    # then resolve a span against text the agent never saw -- a wrong record
    # that passes every later check because it is well formed.
    _seed(tmp_path)
    assignments = mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    projection = Path(assignments[0].read)
    projected = list(read_jsonl_models(projection, UtteranceForJudgment))
    assert [(item.i, item.text) for item in projected] == [(1, _SESSION_ONE), (2, "")]

    # And the identity that used to ride along on every line is not there: the
    # judgment never used it, and a filename is opaque for the same reason.
    raw = projection.read_text(encoding="utf-8")
    assert "1" * 64 not in raw
    assert "c" * 64 not in raw
    assert "claude_code:u1" not in raw


def test_step_d_refuses_to_prepare_before_the_run_consents_to_provider_transfer(
    tmp_path: Path,
) -> None:
    # Preparing this step is the moment the learner's own sentences become
    # something an AI provider will read.
    _seed(tmp_path, provider_transfer_consent=False)
    with pytest.raises(MissingConsentError):
        mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)


def test_a_clean_step_d_file_promotes_the_step_and_names_step_es_files(tmp_path: Path) -> None:
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    # A session where the learner made no mistakes is an empty file.
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert outcome.diagnostics == []
    assert outcome.passed
    assert (outcome.sessions, outcome.records, outcome.sessions_with_records) == (2, 2, 1)
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.PROMOTED
    # The drafts named a line; the file names the utterance, and carries the
    # source type and modality the agent was never asked for.
    assert _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST) == [
        _record(1, _WENT),
        _record(1, _A_BREAD),
    ]

    # Step e has no prepare of its own, so promoting step d is what names its
    # work; the empty session is named too, or nobody would confirm it.
    verified = step_dir(_RUN, StepId.E_VERIFIED, root=tmp_path)
    assert [assignment.name for assignment in outcome.next_step] == [_FIRST, _SECOND]
    assert [assignment.items for assignment in outcome.next_step] == [2, 0]
    assert [Path(a.read) for a in outcome.next_step] == [
        projection_path(verified, _FIRST),
        projection_path(verified, _SECOND),
    ]
    assert [Path(a.write) for a in outcome.next_step] == [
        verdict_path(verified, _FIRST),
        verdict_path(verified, _SECOND),
    ]


def test_an_evidence_span_that_runs_past_its_utterance_fails_the_file(tmp_path: Path) -> None:
    # The span is the only address the record has: the text it quotes is
    # resolved from it later, so a span pointing past the end quotes nothing.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, (7, len(_SESSION_ONE) + 1))])
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_VALUE"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u1:7-54"
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.QUARANTINED


def test_a_draft_addressing_a_line_this_session_does_not_hold_fails_the_file(
    tmp_path: Path,
) -> None:
    # A record borrowed from another session's file is no longer something an
    # agent can write: an index is local to the file it was projected from, and
    # session one holds two lines. What is left is the index that names a third,
    # and it must be resolved against this session rather than against the run --
    # session two's only utterance is a line 1 too.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(3, (3, 12))])
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == f"{_FIRST}:3"
    # Nothing was written for a draft nobody could resolve.
    assert _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST) == []


def test_two_records_covering_the_same_characters_fail_the_file(tmp_path: Path) -> None:
    # This is the double count. Both records are individually well formed and
    # both cite real text, so nothing else in the step notices that the
    # learner's error rate just gained a numerator it did not earn.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, (7, 15)), _draft(1, (10, 20))])
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    # Both records are still counted: the file failed, and nothing was quietly
    # removed to make the counts agree.
    assert outcome.records == 2


def test_two_records_that_meet_without_overlapping_are_both_kept(tmp_path: Path) -> None:
    # A missing article and a wrong verb form can sit side by side in one
    # sentence. An overlap check that compared "same utterance" rather than
    # "same characters" would reject this pair and undercount instead.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT), _draft(1, (11, 20))])
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert outcome.diagnostics == []
    assert outcome.records == 2


def test_a_record_the_privacy_scanner_rejects_fails_the_file_rather_than_being_dropped(
    tmp_path: Path,
) -> None:
    # Dropping it would publish a smaller count that nobody can explain and
    # would hide a defect in the skill that wrote the record: step d's failure
    # rate is only measurable while its failures are still on disk.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    leaks = "Use the past participle, as the fake-team.example guide says."
    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT, rule=leaks), _draft(1, _A_BREAD)])
    _write_drafts(tmp_path, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["PRIVACY_URL_PRESENT"]
    assert outcome.diagnostics[0].item_ref == _record(1, _WENT).record_id
    assert outcome.records == 2
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.QUARANTINED
    # The driver writes the rejected record beside the clean one rather than
    # expanding around it, so the failure is on disk to be read and counted.
    assert _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST) == [
        _record(1, _WENT, rule=leaks),
        _record(1, _A_BREAD),
    ]


def test_a_session_step_d_did_not_answer_fails_because_no_mistakes_is_an_empty_file(
    tmp_path: Path,
) -> None:
    # An absent decision and a clean session are the same thing to a step that
    # reads what it finds, so a session an agent never opened would be
    # published as a session with nothing wrong in it.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT)])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == _SECOND

    _write_drafts(tmp_path, _SECOND, [])
    assert mistakes.apply_mistakes(_RUN, runs_root=tmp_path).passed


def test_a_step_d_file_for_a_session_step_c_never_had_fails_the_step(tmp_path: Path) -> None:
    # No agent can write this file any more, but a rerun can leave one: step c
    # quarantines a session, the corpus loses it, and step d's directory still
    # holds the artifact from the pass before. It has to fail, because its
    # records cite text this run cannot resolve.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [])
    _write_drafts(tmp_path, _SECOND, [])
    _write_artifact(tmp_path, StepId.D_MISTAKES, "session-0003.jsonl", [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    assert outcome.diagnostics[0].item_ref == "session-0003.jsonl"


def test_a_record_id_is_derived_from_its_span_so_a_rerun_names_it_identically(
    tmp_path: Path,
) -> None:
    # Steps d and e agree on which record step e removed only because neither
    # of them chose the name. An ID a model declared would change between
    # reruns and could collide, and dropped.json would stop meaning anything.
    assert "record_id" not in MistakeRecord.model_fields
    payload = json.loads(_record(1, _WENT).model_dump_json())
    with pytest.raises(ValidationError):
        MistakeRecord.model_validate({**payload, "record_id": "chosen-by-a-model"})

    _promote_step_d(tmp_path, [_draft(1, _WENT)])
    first_pass = _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST)
    assert [record.record_id for record in first_pass] == ["claude_code:u1:7-11"]

    assert mistakes.apply_mistakes(_RUN, runs_root=tmp_path).passed
    second_pass = _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST)
    assert [record.record_id for record in second_pass] == [
        record.record_id for record in first_pass
    ]


def test_a_quarantined_step_d_promotes_once_the_agents_repair_their_file(tmp_path: Path) -> None:
    # Failing the file is only the right answer if the run can go on from
    # there: a quarantined step must be able to re-enter work and promote.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, (7, 15)), _draft(1, (10, 20))])
    _write_drafts(tmp_path, _SECOND, [])
    assert not mistakes.apply_mistakes(_RUN, runs_root=tmp_path).passed

    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT)])
    repaired = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert repaired.passed
    assert repaired.records == 1
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.PROMOTED


def test_the_step_d_skill_names_the_models_its_files_really_hold() -> None:
    """The batch projection is gone; the mismatch it used to cause is not.

    ``pipeline/batches.py`` wrote a three-field projection while the skill told
    the agent its lines were ``NormalizedUtterance``. An agent obeying the skill
    validated nothing, reported a clean zero, and told the user their English
    had no mistakes. The two model names in the skill have to be the two the
    agent's own files actually hold.
    """
    skill = (repo_root() / "skills" / "find-english-mistakes" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`UtteranceForJudgment` in `src/glite_english_audit/pipeline/agent_io.py`" in skill
    assert "`MistakeDraft` in `src/glite_english_audit/pipeline/agent_io.py`" in skill
    # The names of files the agent never opens. MistakeRecord is what the driver
    # expands a draft into and NormalizedUtterance is what step c wrote; sending
    # an agent to validate against either describes a file it will never hold.
    assert "`MistakeRecord` in `src/glite_english_audit/artifacts/models.py`" not in skill
    assert "`NormalizedUtterance` in `src/glite_english_audit/artifacts/models.py`" not in skill
    # The pooled pipeline's own names, from two shapes ago.
    assert "AnalysisUtterance" not in skill
    assert "PrivateMistake" not in skill


# --- step e: removal is the only edit it may make --------------------------


def test_step_e_refuses_to_run_before_step_d_is_promoted(tmp_path: Path) -> None:
    # Nothing has yet checked these records against the text they cite, so
    # confirming them would attest to spans no one measured.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write_drafts(tmp_path, _FIRST, [_draft(1, _WENT)])
    _write_drafts(tmp_path, _SECOND, [])

    with pytest.raises(ValueError, match="step d is not promoted"):
        verify.apply_verification(_RUN, runs_root=tmp_path)
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.PENDING


def test_the_step_e_projection_carries_no_utterance_id_and_no_span(tmp_path: Path) -> None:
    """The privacy property this whole shape exists for.

    Step e judges whether a record could identify someone, and the utterance ID
    and the evidence span are local addresses that answer nothing about that.
    The skill used to say so in prose, which is an instruction a model can
    ignore; now they are not in the file it reads, which is not.
    """
    _promote_step_d(tmp_path, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    projection = projection_path(step_dir(_RUN, StepId.E_VERIFIED, root=tmp_path), _FIRST)
    projected = list(read_jsonl_models(projection, RecordForConfidentiality))
    assert [item.i for item in projected] == [1, 2]

    raw = projection.read_text(encoding="utf-8")
    keys = {key for line in raw.splitlines() for key in json.loads(line)}
    assert keys == {"i", "mistake", "rule", "example", "example_type"}

    # The addresses as values and not only as field names, so a leak into one of
    # the five permitted fields is caught too. The driver still holds both, so
    # hiding them costs the run nothing.
    produced = _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST)
    assert [record.record_id for record in produced] == [
        "claude_code:u1:7-11",
        "claude_code:u1:35-42",
    ]
    for record in produced:
        assert record.utterance_id not in raw
        assert record.record_id not in raw
    # `claude_code` also names the tool the learner was working in.
    assert "claude_code" not in raw


def test_a_step_e_verdict_that_drops_nothing_passes_and_confirms_every_record(
    tmp_path: Path,
) -> None:
    _promote_step_d(tmp_path, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    _write_verdict(tmp_path, _FIRST, [])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert outcome.diagnostics == []
    assert outcome.passed
    assert (outcome.records_in, outcome.records_kept, outcome.records_dropped) == (2, 2, 0)
    assert outcome.sessions_affected == 0
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.PROMOTED
    # Written even when empty: "step e removed nothing" is a claim the review
    # reads, not an absence it has to interpret.
    assert _dropped(tmp_path) == {}


def test_a_record_step_e_removes_is_named_in_dropped_json(tmp_path: Path) -> None:
    # The difference between the two files is this run's withheld-for-privacy
    # count, so it has to be written down rather than recomputed from totals.
    # The agent named an index; the report names the record, because an index
    # means nothing to a reader who does not have step d's file open.
    _promote_step_d(tmp_path, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    _write_verdict(tmp_path, _FIRST, [1])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert outcome.passed
    assert (outcome.records_in, outcome.records_kept, outcome.records_dropped) == (2, 1, 1)
    assert outcome.sessions_affected == 1
    assert _dropped(tmp_path) == {_FIRST: ["claude_code:u1:7-11"]}


def test_step_e_cannot_add_alter_repeat_or_reorder_because_the_driver_rebuilds_the_file(
    tmp_path: Path,
) -> None:
    """The four checks that left ``pipeline/verify.py``, restated as one property.

    The driver used to compare each step-e file against its step-d file and
    report a record added, altered, repeated or moved. The agent no longer
    writes records, so none of the four is a thing that can happen: step e's
    file is step d's own lines with some of them left out. Asserting that here
    is what makes deleting those checks safe rather than merely smaller.
    """
    _promote_step_d(tmp_path, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    _write_verdict(tmp_path, _FIRST, [2])
    _write_verdict(tmp_path, _SECOND, [])
    assert verify.apply_verification(_RUN, runs_root=tmp_path).passed

    produced = _lines(tmp_path, StepId.D_MISTAKES, _FIRST)
    # Byte for byte, not model for model: the old shape compared parsed records,
    # so a re-serialized copy passed a check the skill said it should fail.
    assert _lines(tmp_path, StepId.E_VERIFIED, _FIRST) == [produced[0]]


def test_an_index_past_the_end_of_step_ds_file_fails_step_e(tmp_path: Path) -> None:
    # The failure the index format introduces and the record format could not
    # have: a copied record either matched a step-d record or it did not, but a
    # number always parses. Resolved against nothing, it would withhold nothing
    # and the file would pass as a full confirmation.
    _promote_step_d(tmp_path, [_draft(1, _WENT)])
    _write_verdict(tmp_path, _FIRST, [2])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    assert outcome.diagnostics[0].item_ref == f"{_FIRST}:2"
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.QUARANTINED
    assert not _dropped_exists(tmp_path)


def test_an_index_named_twice_fails_step_e(tmp_path: Path) -> None:
    # A set would swallow this, and the two readings it swallows are not the
    # same run: the agent may have meant one record and repeated itself, or
    # meant two and mistyped the second. Nothing here can tell, and guessing
    # decides whether a record the agent wanted withheld gets published.
    _promote_step_d(tmp_path, [_draft(1, _WENT), _draft(1, _A_BREAD)])
    _write_verdict(tmp_path, _FIRST, [1, 1])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    assert outcome.diagnostics[0].item_ref == f"{_FIRST}:1"
    assert "more than once" in outcome.diagnostics[0].message
    assert not _dropped_exists(tmp_path)


def test_a_session_step_e_did_not_answer_fails_instead_of_withholding_all_of_it(
    tmp_path: Path,
) -> None:
    # A missing verdict reads as "every record in this session was withheld",
    # which is a decision an agent that never ran did not make.
    _promote_step_d(tmp_path, [_draft(1, _WENT)])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == _FIRST
    assert outcome.records_dropped == 0
    assert not _dropped_exists(tmp_path)


@pytest.mark.parametrize(
    ("name", "raw", "code"),
    [
        ("truncated mid-object", "{oops\n", "SCHEMA_INVALID_JSON"),
        ("one record per line, the way step e used to answer", _OLD_SHAPE, "SCHEMA_INVALID_VALUE"),
    ],
    # Named, because the second case's raw text is a whole serialized record.
    ids=["truncated", "record-per-line"],
)
def test_a_verdict_that_does_not_parse_as_a_drop_list_fails_step_e(
    tmp_path: Path, name: str, raw: str, code: str
) -> None:
    # An unreadable verdict is read as an empty drop list, which keeps every
    # record. That is only safe because the step then fails: a file nobody could
    # read must not be able to publish the records it was asked to judge.
    _promote_step_d(tmp_path, [_draft(1, _WENT)])
    verdict_path(step_dir(_RUN, StepId.E_VERIFIED, root=tmp_path), _FIRST).write_text(
        raw, encoding="utf-8"
    )
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed, name
    assert _codes(outcome.diagnostics) == [code], name
    assert outcome.diagnostics[0].item_ref == _FIRST
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.QUARANTINED
    assert not _dropped_exists(tmp_path)


def test_a_step_d_file_edited_after_its_own_check_passed_fails_step_e(tmp_path: Path) -> None:
    # Step d's promotion attests to the bytes it checked, and step e's file is
    # now rebuilt from those bytes rather than compared against them. Re-reading
    # them is what makes it the step that notices they changed.
    _promote_step_d(tmp_path, [_draft(1, _WENT)])
    (step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path) / _FIRST).write_text(
        "{not json\n", encoding="utf-8"
    )
    _write_verdict(tmp_path, _FIRST, [])
    _write_verdict(tmp_path, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_JSON"]
    assert not _dropped_exists(tmp_path)
