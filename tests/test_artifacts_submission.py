"""Submission contract behavior: counts, payload hash, and consent envelope."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.models import ModalityCounts, SafeMistakeRecord
from glite_english_audit.artifacts.submission import (
    NewSubmissionRequest,
    SubmissionCounts,
    SubmissionPackage,
    compute_payload_hash,
    verify_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64
_FAKE_SECRET = "0" * 64
_FAKE_SUBMISSION_ID = "sub-" + "ab" * 16


def _zero_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=0,
        analyzed_words=0,
        eligible_utterances=0,
        analyzed_utterances=0,
    )


def _counts(
    *,
    shared: int,
    withheld_by_user: int = 0,
    withheld_for_privacy: int = 0,
    other_withheld: dict[str, int] | None = None,
) -> SubmissionCounts:
    other = other_withheld or {}
    verified = shared + withheld_by_user + withheld_for_privacy + sum(other.values())
    return SubmissionCounts(
        eligible_english_words=200,
        analyzed_english_words=150,
        eligible_utterances=20,
        analyzed_utterances=15,
        written=_zero_modality(),
        spoken_asr=_zero_modality(),
        verified_total_mistakes=verified,
        shared_mistakes=shared,
        withheld_by_user=withheld_by_user,
        withheld_for_privacy=withheld_for_privacy,
        other_withheld=other,
    )


def _record(mistake: str = "Wrote 'more easy' instead of 'easier'.") -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule="Short adjectives form the comparative with -er.",
        example="This route is easier than the old one.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _package(
    records: list[SafeMistakeRecord] | None = None,
    *,
    counts: SubmissionCounts | None = None,
    payload_hash: str = _PLACEHOLDER_HASH,
    client_version: str = "0.1.0",
) -> SubmissionPackage:
    resolved_records = [_record()] if records is None else records
    return SubmissionPackage(
        submission_schema_version=1,
        submission_id=_FAKE_SUBMISSION_ID,
        recovery_secret=_FAKE_SECRET,
        payload_hash=payload_hash,
        client_version=client_version,
        producer_version="0.1.0",
        privacy_verifier_version="0.1.0",
        records=resolved_records,
        counts=counts if counts is not None else _counts(shared=len(resolved_records)),
    )


def test_submission_counts_rejects_unknown_reason_code() -> None:
    with pytest.raises(ValidationError):
        _counts(shared=0, other_withheld={"NOT_A_REGISTERED_CODE": 1})


def test_submission_counts_accepts_registered_reason_codes() -> None:
    counts = _counts(
        shared=1,
        other_withheld={
            "WITHHELD_BY_USER": 1,
            "WITHHELD_PRIVACY_UNSAFE": 2,
            "WITHHELD_PROCESSING_FAILED": 3,
        },
    )
    assert counts.verified_total_mistakes == 7


def test_submission_counts_rejects_negative_reason_count() -> None:
    with pytest.raises(ValidationError):
        _counts(shared=0, other_withheld={"WITHHELD_PROCESSING_FAILED": -1})


def test_submission_counts_requires_arithmetic_identity() -> None:
    with pytest.raises(ValidationError):
        SubmissionCounts(
            eligible_english_words=10,
            analyzed_english_words=10,
            eligible_utterances=1,
            analyzed_utterances=1,
            written=_zero_modality(),
            spoken_asr=_zero_modality(),
            verified_total_mistakes=5,
            shared_mistakes=1,
            withheld_by_user=1,
            withheld_for_privacy=0,
        )


def test_payload_hash_round_trip() -> None:
    draft = _package()
    digest = compute_payload_hash(draft)
    sealed = draft.model_copy(update={"payload_hash": digest})
    assert verify_payload_hash(sealed)
    assert not verify_payload_hash(draft)


def test_payload_hash_excludes_the_hash_field_itself() -> None:
    draft = _package()
    sealed = draft.model_copy(update={"payload_hash": compute_payload_hash(draft)})
    assert compute_payload_hash(sealed) == compute_payload_hash(draft)


def test_payload_hash_is_stable_for_identical_content() -> None:
    assert compute_payload_hash(_package()) == compute_payload_hash(_package())


def test_payload_hash_changes_when_a_record_changes() -> None:
    original = _package([_record()])
    edited = _package([_record(mistake="Wrote 'informations' instead of 'information'.")])
    assert compute_payload_hash(original) != compute_payload_hash(edited)


def test_package_rejects_records_vs_shared_count_mismatch() -> None:
    with pytest.raises(ValidationError):
        _package([_record()], counts=_counts(shared=0))


def test_package_rejects_malformed_identifiers() -> None:
    valid = _package()
    with pytest.raises(ValidationError):
        SubmissionPackage.model_validate(
            {**valid.model_dump(mode="json"), "submission_id": "sub-not-hex"}
        )
    with pytest.raises(ValidationError):
        SubmissionPackage.model_validate({**valid.model_dump(mode="json"), "recovery_secret": "0"})


def _request_payload() -> dict[str, object]:
    return {
        "package": _package().model_dump(mode="json"),
        "adult_attested": True,
        "permanent_storage_and_uses_accepted": True,
        "external_ai_processing_accepted": True,
        "consent_policy_version": "2026-01",
        "client_confirmation_at": "2026-08-08T12:00:00+00:00",
    }


def test_new_submission_request_accepts_full_consent() -> None:
    request = NewSubmissionRequest.model_validate(_request_payload())
    assert request.adult_attested is True
    assert request.client_confirmation_at.tzinfo is not None


@pytest.mark.parametrize(
    "consent_field",
    ["adult_attested", "permanent_storage_and_uses_accepted", "external_ai_processing_accepted"],
)
def test_new_submission_request_requires_literal_true_consents(consent_field: str) -> None:
    payload = _request_payload()
    payload[consent_field] = False
    with pytest.raises(ValidationError):
        NewSubmissionRequest.model_validate(payload)


def test_new_submission_request_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        NewSubmissionRequest(
            package=_package(),
            adult_attested=True,
            permanent_storage_and_uses_accepted=True,
            external_ai_processing_accepted=True,
            consent_policy_version="2026-01",
            client_confirmation_at=datetime(2026, 8, 8, 12, 0, 0),
        )


def test_new_submission_request_accepts_timezone_aware_timestamp() -> None:
    request = NewSubmissionRequest(
        package=_package(),
        adult_attested=True,
        permanent_storage_and_uses_accepted=True,
        external_ai_processing_accepted=True,
        consent_policy_version="2026-01",
        client_confirmation_at=datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC),
    )
    assert request.consent_policy_version == "2026-01"
