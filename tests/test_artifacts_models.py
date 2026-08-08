"""Construction and validator behavior for the stage record models."""

from typing import Any

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.enums import (
    Accessibility,
    ExampleType,
    Modality,
    OsEnvironment,
    Stability,
    StageId,
    TextStatus,
)
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.models import (
    AuditCounts,
    EvidenceSpan,
    InstanceInventorySummary,
    ModalityCounts,
    NormalizedUtterance,
    PrivateMistake,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
    SafeRecordCandidate,
    SourceInstanceRecord,
)

_HEX64 = "0" * 64


def _envelope() -> ArtifactEnvelope:
    return ArtifactEnvelope(
        schema_name="reviewed_submission",
        schema_version=1,
        artifact_id="art-" + "0" * 32,
        run_id="run-" + "0" * 32,
        stage_id=StageId.REVIEWED_SUBMISSION,
        producer_name="test-producer",
        producer_version="0.0.1",
        created_at=utc_now(),
    )


def _source_instance(**overrides: Any) -> SourceInstanceRecord:
    data: dict[str, Any] = {
        "adapter_id": "claude_code",
        "adapter_version": "1.0.0",
        "instance_key": "default",
        "opaque_label": "Source A",
        "storage_format": "jsonl",
        "schema_fingerprint": "v1",
        "path_hash": _HEX64,
        "os_environment": OsEnvironment.MACOS,
        "stability": Stability.STABLE,
        "accessibility": Accessibility.FOUND,
        "estimated_records": 10,
        "candidate_messages": 5,
        "candidate_words": 100,
        "candidate_bytes": 1000,
    }
    data.update(overrides)
    return SourceInstanceRecord(**data)


def _inventory_summary(**overrides: Any) -> InstanceInventorySummary:
    data: dict[str, Any] = {
        "adapter_id": "codex",
        "adapter_version": "1.0.0",
        "opaque_label": "Source B",
        "stability": Stability.BETA,
        "accessibility": Accessibility.FOUND,
        "estimated_records": 3,
        "candidate_messages": 3,
        "candidate_words": 40,
        "candidate_bytes": 400,
    }
    data.update(overrides)
    return InstanceInventorySummary(**data)


def _normalized_utterance(**overrides: Any) -> NormalizedUtterance:
    data: dict[str, Any] = {
        "utterance_id": "utt-0001",
        "source_adapter": "claude_code",
        "adapter_version": "1.0.0",
        "session_hash": _HEX64,
        "text": "I have went to the store.",
        "modality": Modality.WRITTEN,
        "text_status": TextStatus.VERBATIM,
        "authorship_confidence": 0.9,
        "authorship_basis": "role=user",
        "source_path_hash": _HEX64,
    }
    data.update(overrides)
    return NormalizedUtterance(**data)


def _private_mistake(modality: Modality) -> PrivateMistake:
    return PrivateMistake(
        mistake_id="mis-0001",
        occurrence_id="occ-0001",
        finding_artifact_id="art-" + "1" * 32,
        utterance_id="utt-0001",
        evidence_span=EvidenceSpan(start=2, end=11),
        original_text="have went",
        correction="went",
        explanation="Use the simple past, not 'have went'.",
        modality=modality,
        source_adapter="claude_code",
        session_hash=_HEX64,
    )


def _safe_record(**overrides: Any) -> SafeMistakeRecord:
    data: dict[str, Any] = {
        "mistake": "Wrote 'have went' instead of 'went'.",
        "rule": "Use the simple past for a finished action.",
        "example": "Yesterday I went to the store.",
        "example_type": ExampleType.SYNTHETIC,
        "source_type": "claude_code",
        "modality": Modality.WRITTEN,
    }
    data.update(overrides)
    return SafeMistakeRecord(**data)


def _zero_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=0,
        analyzed_words=0,
        eligible_utterances=0,
        analyzed_utterances=0,
    )


def _audit_counts(**overrides: Any) -> AuditCounts:
    data: dict[str, Any] = {
        "eligible_english_words": 120,
        "analyzed_english_words": 100,
        "eligible_utterances": 12,
        "analyzed_utterances": 10,
        "written": ModalityCounts(
            eligible_words=100,
            analyzed_words=90,
            eligible_utterances=10,
            analyzed_utterances=9,
        ),
        "spoken_asr": ModalityCounts(
            eligible_words=20,
            analyzed_words=10,
            eligible_utterances=2,
            analyzed_utterances=1,
        ),
        "verified_total_mistakes": 3,
        "shared_mistakes": 2,
        "withheld_by_user": 1,
        "withheld_for_privacy": 0,
    }
    data.update(overrides)
    return AuditCounts(**data)


def test_source_instance_record_accepts_valid_fields() -> None:
    record = _source_instance()
    assert record.adapter_id == "claude_code"
    assert record.os_environment is OsEnvironment.MACOS
    assert record.app_version is None


@pytest.mark.parametrize("bad_id", ["Claude", "claude-code", "9codex", "", "claude code"])
def test_source_instance_record_rejects_bad_adapter_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _source_instance(adapter_id=bad_id)


@pytest.mark.parametrize("bad_hash", ["", "0" * 63, "z" * 64, "A" * 64])
def test_source_instance_record_rejects_bad_path_hash(bad_hash: str) -> None:
    with pytest.raises(ValidationError):
        _source_instance(path_hash=bad_hash)


@pytest.mark.parametrize("bad_id", ["Codex", "codex-cli", "1cursor", ""])
def test_inventory_summary_rejects_bad_adapter_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _inventory_summary(adapter_id=bad_id)


def test_inventory_summary_accepts_valid_fields() -> None:
    summary = _inventory_summary()
    assert summary.opaque_label == "Source B"
    assert summary.diagnostic_code is None


def test_normalized_utterance_accepts_valid_fields() -> None:
    utterance = _normalized_utterance()
    assert utterance.modality is Modality.WRITTEN
    assert utterance.content_flags == []


def test_normalized_utterance_rejects_bad_source_adapter() -> None:
    with pytest.raises(ValidationError):
        _normalized_utterance(source_adapter="Claude-Code")


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_normalized_utterance_rejects_out_of_range_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        _normalized_utterance(authorship_confidence=confidence)


def test_evidence_span_accepts_ordered_bounds() -> None:
    span = EvidenceSpan(start=0, end=1)
    assert span.start == 0
    assert span.end == 1


@pytest.mark.parametrize(("start", "end"), [(2, 2), (3, 2), (5, 1)])
def test_evidence_span_rejects_end_not_after_start(start: int, end: int) -> None:
    with pytest.raises(ValidationError):
        EvidenceSpan(start=start, end=end)


def test_evidence_span_rejects_negative_start() -> None:
    with pytest.raises(ValidationError):
        EvidenceSpan(start=-1, end=1)


@pytest.mark.parametrize("modality", [Modality.WRITTEN, Modality.SPOKEN_ASR])
def test_private_mistake_accepts_resolved_modality(modality: Modality) -> None:
    assert _private_mistake(modality).modality is modality


def test_private_mistake_rejects_unknown_modality() -> None:
    with pytest.raises(ValidationError):
        _private_mistake(Modality.UNKNOWN)


def test_safe_mistake_record_rejects_unknown_modality() -> None:
    with pytest.raises(ValidationError):
        _safe_record(modality=Modality.UNKNOWN)


@pytest.mark.parametrize("field", ["mistake", "rule", "example"])
@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_safe_mistake_record_rejects_empty_sentences(field: str, blank: str) -> None:
    with pytest.raises(ValidationError):
        _safe_record(**{field: blank})


@pytest.mark.parametrize("bad_source", ["Claude", "claude-code", "", "9tool"])
def test_safe_mistake_record_rejects_bad_source_type(bad_source: str) -> None:
    with pytest.raises(ValidationError):
        _safe_record(source_type=bad_source)


def test_safe_record_candidate_failure_requires_reason_code() -> None:
    with pytest.raises(ValidationError):
        SafeRecordCandidate(
            mistake_id="mis-0001",
            record=_safe_record(),
            creator_version="0.0.1",
            creation_failed=True,
        )


def test_safe_record_candidate_accepts_failure_with_reason_code() -> None:
    candidate = SafeRecordCandidate(
        mistake_id="mis-0001",
        record=_safe_record(),
        creator_version="0.0.1",
        creation_failed=True,
        failure_reason_code="WITHHELD_PROCESSING_FAILED",
    )
    assert candidate.creation_failed
    assert candidate.failure_reason_code == "WITHHELD_PROCESSING_FAILED"


def test_safe_record_candidate_defaults_to_success() -> None:
    candidate = SafeRecordCandidate(
        mistake_id="mis-0001",
        record=_safe_record(),
        creator_version="0.0.1",
    )
    assert not candidate.creation_failed
    assert candidate.failure_reason_code is None


def test_modality_counts_rejects_analyzed_words_over_eligible() -> None:
    with pytest.raises(ValidationError):
        ModalityCounts(
            eligible_words=5,
            analyzed_words=6,
            eligible_utterances=1,
            analyzed_utterances=1,
        )


def test_modality_counts_rejects_analyzed_utterances_over_eligible() -> None:
    with pytest.raises(ValidationError):
        ModalityCounts(
            eligible_words=5,
            analyzed_words=5,
            eligible_utterances=1,
            analyzed_utterances=2,
        )


def test_audit_counts_accepts_consistent_totals() -> None:
    counts = _audit_counts()
    assert counts.verified_total_mistakes == 3


def test_audit_counts_requires_shared_plus_withheld_equals_verified() -> None:
    with pytest.raises(ValidationError):
        _audit_counts(verified_total_mistakes=5)


def test_audit_counts_sums_other_withheld_into_identity() -> None:
    counts = _audit_counts(
        verified_total_mistakes=4,
        other_withheld={"WITHHELD_PROCESSING_FAILED": 1},
    )
    assert counts.other_withheld == {"WITHHELD_PROCESSING_FAILED": 1}


def test_audit_counts_rejects_negative_other_withheld() -> None:
    with pytest.raises(ValidationError):
        _audit_counts(other_withheld={"WITHHELD_PROCESSING_FAILED": -1})


def test_audit_counts_rejects_modality_words_over_total() -> None:
    with pytest.raises(ValidationError):
        _audit_counts(eligible_english_words=100, analyzed_english_words=90)


def test_audit_counts_rejects_analyzed_words_over_eligible() -> None:
    with pytest.raises(ValidationError):
        _audit_counts(analyzed_english_words=121)


def test_audit_counts_rejects_analyzed_utterances_over_eligible() -> None:
    with pytest.raises(ValidationError):
        _audit_counts(analyzed_utterances=13)


def _reviewed_records() -> list[ReviewedRecord]:
    return [
        ReviewedRecord(
            mistake_id="mis-0001",
            record=_safe_record(),
            included=True,
            privacy_creator_version="0.0.1",
            privacy_verifier_version="0.0.1",
        ),
        ReviewedRecord(
            mistake_id="mis-0002",
            record=_safe_record(mistake="Wrote 'more easy' instead of 'easier'."),
            included=False,
            privacy_creator_version="0.0.1",
            privacy_verifier_version="0.0.1",
        ),
    ]


def _reviewed_artifact_counts(*, shared: int, withheld_by_user: int) -> AuditCounts:
    return _audit_counts(
        written=_zero_modality(),
        spoken_asr=_zero_modality(),
        verified_total_mistakes=shared + withheld_by_user,
        shared_mistakes=shared,
        withheld_by_user=withheld_by_user,
        withheld_for_privacy=0,
    )


def test_reviewed_submission_accepts_matching_counts() -> None:
    artifact = ReviewedSubmissionArtifact(
        envelope=_envelope(),
        records=_reviewed_records(),
        counts=_reviewed_artifact_counts(shared=1, withheld_by_user=1),
    )
    assert artifact.counts.shared_mistakes == 1


def test_reviewed_submission_rejects_shared_count_mismatch() -> None:
    with pytest.raises(ValidationError):
        ReviewedSubmissionArtifact(
            envelope=_envelope(),
            records=_reviewed_records(),
            counts=_reviewed_artifact_counts(shared=2, withheld_by_user=0),
        )


def test_reviewed_submission_rejects_withheld_count_mismatch() -> None:
    with pytest.raises(ValidationError):
        ReviewedSubmissionArtifact(
            envelope=_envelope(),
            records=_reviewed_records(),
            counts=_reviewed_artifact_counts(shared=1, withheld_by_user=2),
        )


def test_models_reject_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate({"start": 0, "end": 1, "note": "extra"})
    with pytest.raises(ValidationError):
        SafeMistakeRecord.model_validate(
            {**_safe_record().model_dump(mode="json"), "session_hash": _HEX64}
        )
    with pytest.raises(ValidationError):
        _source_instance(source_path="/somewhere/private")
