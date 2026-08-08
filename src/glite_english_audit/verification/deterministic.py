"""Deterministic cross-artifact verifiers.

Everything checkable without model judgment (specification, 6.3): file hashes
against manifests, count arithmetic, lineage freshness against the run
manifest, and the submission boundary.
"""

from pathlib import Path

from glite_english_audit.artifacts.envelope import ArtifactEnvelope
from glite_english_audit.artifacts.hashing import sha256_hex
from glite_english_audit.artifacts.manifest import RunManifest
from glite_english_audit.artifacts.models import ReviewedSubmissionArtifact
from glite_english_audit.artifacts.submission import (
    SubmissionCounts,
    SubmissionPackage,
    verify_payload_hash,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.verification.privacy_scanner import scan_safe_record


def verify_file_hash(path: Path, expected_sha256: str, *, item_ref: str) -> list[Diagnostic]:
    """Check that a referenced file exists and matches its recorded hash."""
    if not path.is_file():
        return [
            Diagnostic.from_code(
                "LINEAGE_MISSING_INPUT",
                f"referenced file is missing: {item_ref}",
                item_ref=item_ref,
            )
        ]
    actual = sha256_hex(path.read_bytes())
    if actual != expected_sha256:
        return [
            Diagnostic.from_code(
                "LINEAGE_HASH_MISMATCH",
                f"file bytes do not match the recorded hash: {item_ref}",
                item_ref=item_ref,
            )
        ]
    return []


def verify_lineage(envelope: ArtifactEnvelope, manifest: RunManifest) -> list[Diagnostic]:
    """Check that an artifact's inputs are the manifest's current artifacts."""
    diagnostics: list[Diagnostic] = []
    current_ids = {
        state.current_artifact_id
        for state in manifest.stages.values()
        if state.current_artifact_id is not None
    }
    current_hashes = {
        state.current_artifact_hash
        for state in manifest.stages.values()
        if state.current_artifact_hash is not None
    }
    for input_id in envelope.input_artifact_ids:
        if input_id not in current_ids:
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_STALE_REFERENCE",
                    f"input artifact {input_id} is not a current artifact in the run manifest",
                    item_ref=envelope.artifact_id,
                )
            )
    for input_id, input_hash in envelope.input_hashes.items():
        if input_hash not in current_hashes:
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_STALE_REFERENCE",
                    f"input hash recorded for {input_id} was replaced in the run manifest",
                    item_ref=envelope.artifact_id,
                )
            )
    return diagnostics


def verify_submission_package(package: SubmissionPackage) -> list[Diagnostic]:
    """The complete deterministic gate for one submission package."""
    diagnostics: list[Diagnostic] = []
    if not verify_payload_hash(package):
        diagnostics.append(
            Diagnostic.from_code(
                "SUBMISSION_HASH_MISMATCH",
                "payload_hash does not match the canonical hash of the package contents",
            )
        )
    if not package.records:
        diagnostics.append(
            Diagnostic.from_code(
                "SUBMISSION_NO_RECORDS",
                "the package contains no detailed mistake record",
            )
        )
    for index, record in enumerate(package.records):
        diagnostics.extend(scan_safe_record(record, item_ref=f"records[{index}]"))
    return diagnostics


def verify_package_against_review(
    package: SubmissionPackage,
    reviewed: ReviewedSubmissionArtifact,
) -> list[Diagnostic]:
    """Check the exported package against the reviewed local artifact."""
    diagnostics: list[Diagnostic] = []
    included_records = [entry.record for entry in reviewed.records if entry.included]
    if list(package.records) != included_records:
        diagnostics.append(
            Diagnostic.from_code(
                "SUBMISSION_COUNT_MISMATCH",
                "package records do not equal the included records of the reviewed artifact",
            )
        )
    expected_counts = SubmissionCounts.from_audit_counts(reviewed.counts)
    if package.counts != expected_counts:
        diagnostics.append(
            Diagnostic.from_code(
                "SUBMISSION_COUNT_MISMATCH",
                "package counts do not equal the reviewed artifact counts",
            )
        )
    return diagnostics
