"""Steps d and e: records that are clean when written, then a reader that may only remove.

These two steps decide the number the product publishes. Step d's records are
the numerator of the learner's error rate, so every way that numerator can be
wrong belongs here: a span that addresses text nobody wrote, a record borrowed
from another session's file, and two records billing the same characters twice.

The rule these tests exist to hold is that step d fails a file rather than
repairing it. A dropped record leaves a smaller count with nothing to explain
it, which turns a defect in the producing skill into a published number and
makes step d's own failure rate unmeasurable.

Step e is the second reader, and the reason a run with step e removed would
still be right is that step e may only delete. So the four ways it can become a
second author -- adding, altering, repeating, reordering -- each have to arrive
as their own diagnostic instead of being absorbed into the withheld count.

There is no batch projection to test any more: the agent reads the session file
itself, so what these tests fake is the agent, by writing the file it would
have written.
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
from glite_english_audit.artifacts.io import ensure_private_dir, write_jsonl_models, write_model
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


def _record(
    index: int,
    span: tuple[int, int],
    *,
    rule: str = "Use the article before a singular countable noun.",
    mistake: str = "The article is missing before a countable noun.",
) -> MistakeRecord:
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


def _write(runs_root: Path, step: StepId, name: str, records: list[MistakeRecord]) -> Path:
    """Stand in for the agent that writes one session's file."""
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


def _status(runs_root: Path, step: StepId) -> StepStatus:
    return load_manifest(_RUN, root=runs_root).steps[step].status


def _promote_step_d(runs_root: Path, first: list[MistakeRecord]) -> None:
    """Reach the state step e starts from: a promoted step d holding ``first``."""
    _seed(runs_root)
    mistakes.prepare_mistakes(_RUN, runs_root=runs_root)
    _write(runs_root, StepId.D_MISTAKES, _FIRST, first)
    _write(runs_root, StepId.D_MISTAKES, _SECOND, [])
    outcome = mistakes.apply_mistakes(_RUN, runs_root=runs_root)
    assert outcome.passed, _codes(outcome.diagnostics)


def _dropped(runs_root: Path) -> dict[str, list[str]]:
    path = step_dir(_RUN, StepId.E_VERIFIED, root=runs_root) / verify.DROPPED_NAME
    payload: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))["dropped"]
    return payload


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

    source = step_dir(_RUN, StepId.C_AUTHORED, root=tmp_path)
    target = step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path)
    assert [assignment.name for assignment in assignments] == [_FIRST, _SECOND]
    # One file keeps its name across the two steps, so read and write differ
    # only by directory: swapping them sends every agent to read the empty file
    # it was supposed to write.
    assert [Path(a.read) for a in assignments] == [source / _FIRST, source / _SECOND]
    assert [Path(a.write) for a in assignments] == [target / _FIRST, target / _SECOND]
    # Session one holds two utterances and one of them is empty: an agent sent
    # to read two items in that file would spend a turn on nothing.
    assert [assignment.items for assignment in assignments] == [1, 1]
    assert [assignment.words for assignment in assignments] == [
        count_words(_SESSION_ONE),
        count_words(_SESSION_TWO),
    ]


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
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, _WENT), _record(1, _A_BREAD)])
    # A session where the learner made no mistakes is an empty file.
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert outcome.diagnostics == []
    assert outcome.passed
    assert (outcome.sessions, outcome.records, outcome.sessions_with_records) == (2, 2, 1)
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.PROMOTED
    # Step e has no prepare of its own, so promoting step d is what names its
    # work; the empty session is named too, or nobody would confirm it.
    assert [assignment.name for assignment in outcome.next_step] == [_FIRST, _SECOND]
    assert [assignment.items for assignment in outcome.next_step] == [2, 0]


def test_an_evidence_span_that_runs_past_its_utterance_fails_the_file(tmp_path: Path) -> None:
    # The span is the only address the record has: the text it quotes is
    # resolved from it later, so a span pointing past the end quotes nothing.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, (7, len(_SESSION_ONE) + 1))])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_VALUE"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u1:7-54"
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.QUARANTINED


def test_a_record_citing_an_utterance_from_another_session_fails_the_file(tmp_path: Path) -> None:
    # u3 exists in this run, in session two's file. Checking a record against
    # every utterance in the run instead of against its own session's file
    # would accept this, and the span would then be measured against text the
    # session never contained.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(3, (3, 12))])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u3:3-12"


def test_two_records_covering_the_same_characters_fail_the_file(tmp_path: Path) -> None:
    # This is the double count. Both records are individually well formed and
    # both cite real text, so nothing else in the step notices that the
    # learner's error rate just gained a numerator it did not earn.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, (7, 15)), _record(1, (10, 20))])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

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
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, _WENT), _record(1, (11, 20))])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

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
    leaky = _record(1, _WENT, rule="Use the past participle, as the fake-team.example guide says.")
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [leaky, _record(1, _A_BREAD)])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["PRIVACY_URL_PRESENT"]
    assert outcome.diagnostics[0].item_ref == leaky.record_id
    assert outcome.records == 2
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.QUARANTINED
    # The file is the agents' to repair, so it is left exactly as written.
    assert _on_disk(tmp_path, StepId.D_MISTAKES, _FIRST) == [leaky, _record(1, _A_BREAD)]


def test_a_session_step_d_did_not_answer_fails_because_no_mistakes_is_an_empty_file(
    tmp_path: Path,
) -> None:
    # An absent file and a clean session are the same thing to a step that
    # reads what it finds, so a session an agent never opened would be
    # published as a session with nothing wrong in it.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, _WENT)])

    outcome = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == _SECOND

    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])
    assert mistakes.apply_mistakes(_RUN, runs_root=tmp_path).passed


def test_a_step_d_file_for_a_session_step_c_never_had_fails_the_step(tmp_path: Path) -> None:
    # The other direction: an invented file means the two steps are describing
    # different runs, and its records cite text this run cannot resolve.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])
    _write(tmp_path, StepId.D_MISTAKES, "session-0003.jsonl", [])

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

    _promote_step_d(tmp_path, [_record(1, _WENT)])
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
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, (7, 15)), _record(1, (10, 20))])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])
    assert not mistakes.apply_mistakes(_RUN, runs_root=tmp_path).passed

    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, _WENT)])
    repaired = mistakes.apply_mistakes(_RUN, runs_root=tmp_path)
    assert repaired.passed
    assert repaired.records == 1
    assert _status(tmp_path, StepId.D_MISTAKES) is StepStatus.PROMOTED


def test_the_step_d_skill_names_the_models_its_files_really_hold() -> None:
    """The batch projection is gone; the mismatch it used to cause is not.

    ``pipeline/batches.py`` wrote a three-field projection while the skill told
    the agent its lines were ``NormalizedUtterance``. An agent obeying the skill
    validated nothing, reported a clean zero, and told the user their English
    had no mistakes. The agent now opens the session file itself, so the two
    model names in the skill have to be the two these steps actually write.
    """
    skill = (repo_root() / "skills" / "find-english-mistakes" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`NormalizedUtterance` in `src/glite_english_audit/artifacts/models.py`" in skill
    assert "`MistakeRecord` in `src/glite_english_audit/artifacts/models.py`" in skill
    # The pooled pipeline's own names. A skill drifting back to either would
    # send the agent to validate against a model no file here contains.
    assert "AnalysisUtterance" not in skill
    assert "PrivateMistake" not in skill


# --- step e: removal is the only edit it may make --------------------------


def test_step_e_refuses_to_run_before_step_d_is_promoted(tmp_path: Path) -> None:
    # Nothing has yet checked these records against the text they cite, so
    # confirming them would attest to spans no one measured.
    _seed(tmp_path)
    mistakes.prepare_mistakes(_RUN, runs_root=tmp_path)
    _write(tmp_path, StepId.D_MISTAKES, _FIRST, [_record(1, _WENT)])
    _write(tmp_path, StepId.D_MISTAKES, _SECOND, [])

    with pytest.raises(ValueError, match="step d is not promoted"):
        verify.apply_verification(_RUN, runs_root=tmp_path)
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.PENDING


def test_a_step_e_file_that_confirms_every_record_passes_and_drops_nothing(
    tmp_path: Path,
) -> None:
    _promote_step_d(tmp_path, [_record(1, _WENT), _record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [_record(1, _WENT), _record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

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
    _promote_step_d(tmp_path, [_record(1, _WENT), _record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [_record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert outcome.passed
    assert (outcome.records_in, outcome.records_kept, outcome.records_dropped) == (2, 1, 1)
    assert outcome.sessions_affected == 1
    assert _dropped(tmp_path) == {_FIRST: ["claude_code:u1:7-11"]}


def test_a_record_step_e_added_fails_instead_of_counting_as_a_drop(tmp_path: Path) -> None:
    # An added record is a claim nothing checked: the span checks ran against
    # the file step d wrote, and this record was not in it.
    _promote_step_d(tmp_path, [_record(1, _WENT)])
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [_record(1, _WENT), _record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_VALUE"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u1:35-42"
    assert outcome.records_dropped == 0
    assert _status(tmp_path, StepId.E_VERIFIED) is StepStatus.QUARANTINED
    assert not (step_dir(_RUN, StepId.E_VERIFIED, root=tmp_path) / verify.DROPPED_NAME).exists()


def test_a_record_step_e_altered_fails_instead_of_counting_as_a_drop(tmp_path: Path) -> None:
    # The address is untouched, so a check that matched on record_id alone
    # would call this a faithful copy while the published sentence changed.
    _promote_step_d(tmp_path, [_record(1, _WENT)])
    rewritten = _record(1, _WENT, mistake="The verb is in the wrong tense.")
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [rewritten])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_VALUE"]
    assert outcome.diagnostics[0].item_ref == rewritten.record_id
    assert outcome.records_dropped == 0


def test_a_record_step_e_repeated_fails_instead_of_counting_as_a_drop(tmp_path: Path) -> None:
    # Every copy would be shared, so one mistake would be published twice --
    # the same double count step d's overlap rule exists to prevent.
    _promote_step_d(tmp_path, [_record(1, _WENT)])
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [_record(1, _WENT), _record(1, _WENT)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u1:7-11"
    assert outcome.records_kept == 2
    assert outcome.records_dropped == 0


def test_records_step_e_reordered_fail_instead_of_passing_as_an_unchanged_copy(
    tmp_path: Path,
) -> None:
    # Same records, same count, same set: only the order changed, and order is
    # what lets a person diff an e file against its d file line by line.
    _promote_step_d(tmp_path, [_record(1, _WENT), _record(1, _A_BREAD)])
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [_record(1, _A_BREAD), _record(1, _WENT)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["CARDINALITY_MISMATCH"]
    assert outcome.diagnostics[0].item_ref == "claude_code:u1:7-11"
    assert outcome.records_dropped == 0


def test_a_session_step_e_did_not_answer_fails_instead_of_withholding_all_of_it(
    tmp_path: Path,
) -> None:
    # A missing file reads as "every record in this session was withheld",
    # which is a decision an agent that never ran did not make.
    _promote_step_d(tmp_path, [_record(1, _WENT)])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["LINEAGE_MISSING_INPUT"]
    assert outcome.diagnostics[0].item_ref == _FIRST
    assert outcome.records_dropped == 0


def test_a_step_d_file_edited_after_its_own_check_passed_fails_step_e(tmp_path: Path) -> None:
    # Step d's promotion attests to the bytes it checked. Step e re-reads them,
    # so it is the step that notices when they are no longer those bytes.
    _promote_step_d(tmp_path, [_record(1, _WENT)])
    (step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path) / _FIRST).write_text(
        "{not json\n", encoding="utf-8"
    )
    _write(tmp_path, StepId.E_VERIFIED, _FIRST, [])
    _write(tmp_path, StepId.E_VERIFIED, _SECOND, [])

    outcome = verify.apply_verification(_RUN, runs_root=tmp_path)
    assert not outcome.passed
    assert _codes(outcome.diagnostics) == ["SCHEMA_INVALID_JSON"]
