"""Package materializer: allowlist build, hash sealing, and the privacy gate."""

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality, StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.models import (
    AuditCounts,
    ModalityCounts,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
)
from glite_english_audit.artifacts.submission import SubmissionPackage, verify_payload_hash
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.submission import package as package_module
from glite_english_audit.submission.package import MaterializationError, materialize_package

_RUN_ID = "run-" + "0" * 32
_SUBMISSION_ID = "sub-" + "ab" * 16
_RECOVERY_SECRET = "cd" * 32


def _envelope() -> ArtifactEnvelope:
    return ArtifactEnvelope(
        schema_name="reviewed_submission",
        schema_version=1,
        artifact_id="art-" + "11" * 16,
        run_id=_RUN_ID,
        step_id=StepId.E_VERIFIED,
        producer_name="test-factory",
        producer_version="1.0.0",
        created_at=utc_now(),
    )


def _safe_record(
    mistake: str = "The learner wrote very like instead of really like.",
    *,
    example: str = "I really like this plan.",
) -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule="Use really, not very, before like.",
        example=example,
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _written_counts() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=60,
        analyzed_words=50,
        eligible_utterances=6,
        analyzed_utterances=5,
    )


def _spoken_counts() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=40,
        analyzed_words=30,
        eligible_utterances=4,
        analyzed_utterances=3,
    )


def _reviewed(
    records: list[tuple[SafeMistakeRecord, bool]],
    *,
    creator_version: str = "1.2.0",
    verifier_version: str = "1.1.0",
) -> ReviewedSubmissionArtifact:
    reviewed_records = [
        ReviewedRecord(
            mistake_id=f"m-{index:03d}",
            record=record,
            included=included,
            privacy_creator_version=creator_version,
            privacy_verifier_version=verifier_version,
        )
        for index, (record, included) in enumerate(records)
    ]
    included_count = sum(1 for _, included in records if included)
    excluded_count = len(records) - included_count
    counts = AuditCounts(
        eligible_english_words=100,
        analyzed_english_words=80,
        eligible_utterances=10,
        analyzed_utterances=8,
        written=_written_counts(),
        spoken_asr=_spoken_counts(),
        verified_total_mistakes=len(records),
        shared_mistakes=included_count,
        withheld_by_user=excluded_count,
        withheld_for_privacy=0,
    )
    return ReviewedSubmissionArtifact(envelope=_envelope(), records=reviewed_records, counts=counts)


def _mixed_reviewed() -> ReviewedSubmissionArtifact:
    return _reviewed(
        [
            (_safe_record(), True),
            (_safe_record("The learner wrote informations instead of information."), True),
            (_safe_record("The learner wrote advices instead of advice."), False),
        ]
    )


def test_materialize_mixed_records_keeps_only_included() -> None:
    reviewed = _mixed_reviewed()
    package = materialize_package(
        reviewed,
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert len(package.records) == 2
    assert package.records == [entry.record for entry in reviewed.records if entry.included]
    assert package.counts.shared_mistakes == 2
    assert package.counts.withheld_by_user == 1
    assert package.counts.verified_total_mistakes == 3


def test_materialized_payload_hash_verifies() -> None:
    package = materialize_package(
        _mixed_reviewed(),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert verify_payload_hash(package)


def test_injected_identifiers_respected() -> None:
    package = materialize_package(
        _mixed_reviewed(),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert package.submission_id == _SUBMISSION_ID
    assert package.recovery_secret == _RECOVERY_SECRET


def test_generated_identifiers_are_well_formed() -> None:
    package = materialize_package(_mixed_reviewed())
    assert package.submission_id.startswith("sub-")
    assert len(package.recovery_secret) == 64
    assert verify_payload_hash(package)


def test_producer_versions_taken_from_included_entries() -> None:
    package = materialize_package(
        _mixed_reviewed(),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert package.producer_version == "1.2.0"
    assert package.privacy_verifier_version == "1.1.0"


def test_changed_decisions_change_the_payload_hash() -> None:
    records = [
        (_safe_record(), True),
        (_safe_record("The learner wrote informations instead of information."), True),
    ]
    fewer = [
        (_safe_record(), True),
        (_safe_record("The learner wrote informations instead of information."), False),
    ]
    package_all = materialize_package(
        _reviewed(records),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    package_fewer = materialize_package(
        _reviewed(fewer),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert package_all.payload_hash != package_fewer.payload_hash


def test_zero_included_records_raises() -> None:
    reviewed = _reviewed([(_safe_record(), False), (_safe_record("Second synthetic slip."), False)])
    with pytest.raises(MaterializationError) as excinfo:
        materialize_package(
            reviewed,
            submission_id=_SUBMISSION_ID,
            recovery_secret=_RECOVERY_SECRET,
        )
    assert "SUBMISSION_NO_RECORDS" in {d.code for d in excinfo.value.diagnostics}


@pytest.mark.parametrize("field", ["creator_version", "verifier_version"])
def test_free_form_version_never_reaches_the_package(field: str) -> None:
    leaky = "/Users/alice/work/acme-secret-merger  session 9f3a  raw: we ship on Tuesday"
    records = [(_safe_record(), True)]
    reviewed = (
        _reviewed(records, creator_version=leaky)
        if field == "creator_version"
        else _reviewed(records, verifier_version=leaky)
    )
    with pytest.raises(MaterializationError) as excinfo:
        materialize_package(
            reviewed,
            submission_id=_SUBMISSION_ID,
            recovery_secret=_RECOVERY_SECRET,
        )
    codes = {d.code for d in excinfo.value.diagnostics}
    assert "SUBMISSION_FORBIDDEN_FIELD" in codes
    for diagnostic in excinfo.value.diagnostics:
        assert "acme-secret-merger" not in diagnostic.message
        assert "Tuesday" not in diagnostic.message


def test_record_with_invisible_character_fails_the_gate() -> None:
    leaky = _safe_record(example="Write to alice\u200b@acme\u200b.com please.")
    reviewed = _reviewed([(leaky, True)])
    with pytest.raises(MaterializationError) as excinfo:
        materialize_package(
            reviewed,
            submission_id=_SUBMISSION_ID,
            recovery_secret=_RECOVERY_SECRET,
        )
    codes = {d.code for d in excinfo.value.diagnostics}
    assert "PRIVACY_INVISIBLE_CHARACTER" in codes
    assert "PRIVACY_EMAIL_PRESENT" in codes


def test_record_with_email_fails_the_gate() -> None:
    leaky = _safe_record(example="Send it to fake.person@example.com please.")
    reviewed = _reviewed([(leaky, True)])
    with pytest.raises(MaterializationError) as excinfo:
        materialize_package(
            reviewed,
            submission_id=_SUBMISSION_ID,
            recovery_secret=_RECOVERY_SECRET,
        )
    codes = {d.code for d in excinfo.value.diagnostics}
    assert "PRIVACY_EMAIL_PRESENT" in codes
    # The leaked address must not be echoed into the error diagnostics.
    assert all("fake.person@example.com" not in d.message for d in excinfo.value.diagnostics)


def test_the_materializer_runs_the_package_against_review_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second gate is wired in, not merely unit-tested next door.

    ``verify_package_against_review`` is what catches a package whose records
    or counts stopped matching the artifact the user actually reviewed. Testing
    it directly proves the function works; only this proves the materializer
    asks it.
    """
    called: list[str] = []

    def _tripwire(package: SubmissionPackage, reviewed: ReviewedSubmissionArtifact) -> list[object]:
        called.append(package.submission_id)
        return [
            Diagnostic.from_code(
                "SUBMISSION_COUNT_MISMATCH",
                "tripwire: the materializer reached the package-against-review gate",
            )
        ]

    monkeypatch.setattr(package_module, "verify_package_against_review", _tripwire)
    reviewed = _reviewed([(_safe_record(), True)])

    with pytest.raises(MaterializationError) as excinfo:
        materialize_package(
            reviewed,
            submission_id=_SUBMISSION_ID,
            recovery_secret=_RECOVERY_SECRET,
        )

    assert called == [_SUBMISSION_ID]
    assert [d.code for d in excinfo.value.diagnostics] == ["SUBMISSION_COUNT_MISMATCH"]
