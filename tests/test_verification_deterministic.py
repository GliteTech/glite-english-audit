"""Deterministic cross-artifact verifiers: hashes, lineage, submission gate."""

from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    Modality,
    OsEnvironment,
    RunStatus,
    StageId,
)
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import sha256_hex
from glite_english_audit.artifacts.manifest import (
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.artifacts.models import (
    AuditCounts,
    ModalityCounts,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
)
from glite_english_audit.artifacts.submission import (
    SubmissionCounts,
    SubmissionPackage,
    compute_payload_hash,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_file_hash,
    verify_lineage,
    verify_package_against_review,
    verify_submission_package,
)

_RUN_ID = "run-" + "0" * 32
_SUBMISSION_ID = "sub-" + "ab" * 16
_RECOVERY_SECRET = "cd" * 32


def _codes(diagnostics: list[Diagnostic]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _envelope(
    *,
    input_artifact_ids: list[str] | None = None,
    input_hashes: dict[str, str] | None = None,
) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        schema_name="reviewed_submission",
        schema_version=1,
        artifact_id="art-" + "11" * 16,
        run_id=_RUN_ID,
        stage_id=StageId.REVIEWED_SUBMISSION,
        producer_name="test-factory",
        producer_version="1.0.0",
        input_artifact_ids=input_artifact_ids or [],
        input_hashes=input_hashes or {},
        created_at=utc_now(),
    )


def _manifest(*, artifact_id: str, artifact_hash: str) -> RunManifest:
    stages = empty_stage_map()
    state = stages[StageId.PRIVACY_APPROVED]
    state.current_artifact_id = artifact_id
    state.current_artifact_hash = artifact_hash
    return RunManifest(
        manifest_schema_version=1,
        run_id=_RUN_ID,
        created_at=utc_now(),
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.PROCESSING,
        consent=ConsentState(consent_policy_version="2026-01"),
        stages=stages,
        fingerprint=CompatibilityFingerprint(
            adapter_versions={},
            artifact_schema_version=1,
            tokenizer_version="1.0.0",
            skill_versions={},
            prompt_versions={},
            model_ids={},
            consent_policy_version="2026-01",
        ),
    )


def _safe_record(
    mistake: str = "The learner wrote very like instead of really like.",
) -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule="Use really, not very, before like.",
        example="I really like this plan.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _audit_counts(
    *, shared: int, withheld_by_user: int, withheld_for_privacy: int = 0
) -> AuditCounts:
    return AuditCounts(
        eligible_english_words=100,
        analyzed_english_words=80,
        eligible_utterances=10,
        analyzed_utterances=8,
        written=ModalityCounts(
            eligible_words=60,
            analyzed_words=50,
            eligible_utterances=6,
            analyzed_utterances=5,
        ),
        spoken_asr=ModalityCounts(
            eligible_words=40,
            analyzed_words=30,
            eligible_utterances=4,
            analyzed_utterances=3,
        ),
        verified_total_mistakes=shared + withheld_by_user + withheld_for_privacy,
        shared_mistakes=shared,
        withheld_by_user=withheld_by_user,
        withheld_for_privacy=withheld_for_privacy,
    )


def _reviewed(
    records: list[tuple[SafeMistakeRecord, bool]],
    *,
    withheld_for_privacy: int = 0,
) -> ReviewedSubmissionArtifact:
    reviewed_records = [
        ReviewedRecord(
            mistake_id=f"m-{index:03d}",
            record=record,
            included=included,
            privacy_creator_version="1.0.0",
            privacy_verifier_version="1.0.0",
        )
        for index, (record, included) in enumerate(records)
    ]
    included_count = sum(1 for _, included in records if included)
    return ReviewedSubmissionArtifact(
        envelope=_envelope(),
        records=reviewed_records,
        counts=_audit_counts(
            shared=included_count,
            withheld_by_user=len(records) - included_count,
            withheld_for_privacy=withheld_for_privacy,
        ),
    )


def _package() -> SubmissionPackage:
    return materialize_package(
        _reviewed([(_safe_record(), True)]),
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )


def test_verify_file_hash_ok(tmp_path: Path) -> None:
    payload = b"synthetic bytes"
    target = tmp_path / "artifact.json"
    target.write_bytes(payload)
    assert verify_file_hash(target, sha256_hex(payload), item_ref="artifact.json") == []


def test_verify_file_hash_missing(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    diagnostics = verify_file_hash(target, "0" * 64, item_ref="absent.json")
    assert _codes(diagnostics) == ["LINEAGE_MISSING_INPUT"]


def test_verify_file_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(b"synthetic bytes")
    diagnostics = verify_file_hash(target, "0" * 64, item_ref="artifact.json")
    assert _codes(diagnostics) == ["LINEAGE_HASH_MISMATCH"]


def test_verify_lineage_current_inputs_pass() -> None:
    current_hash = "aa" * 32
    manifest = _manifest(artifact_id="art-" + "22" * 16, artifact_hash=current_hash)
    envelope = _envelope(
        input_artifact_ids=["art-" + "22" * 16],
        input_hashes={"art-" + "22" * 16: current_hash},
    )
    assert verify_lineage(envelope, manifest) == []


def test_verify_lineage_stale_artifact_id() -> None:
    manifest = _manifest(artifact_id="art-" + "22" * 16, artifact_hash="aa" * 32)
    envelope = _envelope(input_artifact_ids=["art-" + "33" * 16])
    diagnostics = verify_lineage(envelope, manifest)
    assert _codes(diagnostics) == ["LINEAGE_STALE_REFERENCE"]
    assert "art-" + "33" * 16 in diagnostics[0].message


def test_verify_lineage_stale_input_hash() -> None:
    manifest = _manifest(artifact_id="art-" + "22" * 16, artifact_hash="aa" * 32)
    envelope = _envelope(
        input_artifact_ids=["art-" + "22" * 16],
        input_hashes={"art-" + "22" * 16: "bb" * 32},
    )
    diagnostics = verify_lineage(envelope, manifest)
    assert _codes(diagnostics) == ["LINEAGE_STALE_REFERENCE"]
    assert "replaced" in diagnostics[0].message


def test_verify_submission_package_valid() -> None:
    assert verify_submission_package(_package()) == []


def test_verify_submission_package_tampered_hash() -> None:
    tampered = _package().model_copy(update={"payload_hash": "1" * 64})
    diagnostics = verify_submission_package(tampered)
    assert _codes(diagnostics) == ["SUBMISSION_HASH_MISMATCH"]


def test_verify_submission_package_empty_records() -> None:
    draft = SubmissionPackage(
        submission_schema_version=1,
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
        payload_hash="0" * 64,
        client_version="0.1.0",
        producer_version="1.0.0",
        privacy_verifier_version="1.0.0",
        records=[],
        counts=SubmissionCounts.from_audit_counts(_audit_counts(shared=0, withheld_by_user=1)),
    )
    sealed = draft.model_copy(update={"payload_hash": compute_payload_hash(draft)})
    diagnostics = verify_submission_package(sealed)
    assert _codes(diagnostics) == ["SUBMISSION_NO_RECORDS"]


@pytest.mark.parametrize(
    "field", ["producer_version", "privacy_verifier_version", "client_version"]
)
def test_verify_submission_package_flags_free_form_version(field: str) -> None:
    # A package can reach the gate from disk or from another machine, so the
    # gate re-checks the version fields instead of trusting the materializer.
    leaky = "/Users/alice/work/acme-secret-merger  session 9f3a  raw: we ship on Tuesday"
    tampered = _package().model_copy(update={field: leaky})
    sealed = tampered.model_copy(update={"payload_hash": compute_payload_hash(tampered)})
    diagnostics = verify_submission_package(sealed)
    assert "SUBMISSION_FORBIDDEN_FIELD" in _codes(diagnostics)
    for diagnostic in diagnostics:
        assert "acme-secret-merger" not in diagnostic.message
        assert "Tuesday" not in diagnostic.message


def test_verify_submission_package_accepts_plain_versions() -> None:
    package = _package().model_copy(
        update={"producer_version": "1.4", "privacy_verifier_version": "2"}
    )
    sealed = package.model_copy(update={"payload_hash": compute_payload_hash(package)})
    assert verify_submission_package(sealed) == []


def test_verify_submission_package_flags_unknown_source_type() -> None:
    leaky = "acme_health_oncology_billing_migration_q3_client_novartis"
    package = _package()
    record = package.records[0].model_copy(update={"source_type": leaky})
    tampered = package.model_copy(update={"records": [record]})
    sealed = tampered.model_copy(update={"payload_hash": compute_payload_hash(tampered)})
    diagnostics = verify_submission_package(sealed)
    assert "SCHEMA_INVALID_VALUE" in _codes(diagnostics)
    assert all(leaky not in diagnostic.message for diagnostic in diagnostics)


def test_verify_package_against_review_matches() -> None:
    reviewed = _reviewed([(_safe_record(), True)])
    package = materialize_package(
        reviewed,
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
    )
    assert verify_package_against_review(package, reviewed) == []


def test_verify_package_against_review_record_mismatch() -> None:
    package = _package()
    other = _reviewed(
        [(_safe_record("The learner wrote informations instead of information."), True)]
    )
    diagnostics = verify_package_against_review(package, other)
    assert _codes(diagnostics) == ["SUBMISSION_COUNT_MISMATCH"]
    assert "records" in diagnostics[0].message


def test_verify_package_against_review_count_mismatch() -> None:
    package = _package()
    other = _reviewed([(_safe_record(), True)], withheld_for_privacy=1)
    diagnostics = verify_package_against_review(package, other)
    assert _codes(diagnostics) == ["SUBMISSION_COUNT_MISMATCH"]
    assert "counts" in diagnostics[0].message
