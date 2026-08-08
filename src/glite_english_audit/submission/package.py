"""Materialize the exported submission package from the reviewed artifact.

This is the allowlist boundary (specification, 8.3): the package is built
field by field from an explicit list. Nothing is copied wholesale from a
private artifact, so a new private field can never leak by default. The
materializer validates the reviewed artifact, builds the package, and refuses
to return a package that fails the deterministic submission gate.
"""

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.hashing import new_recovery_secret, new_submission_id
from glite_english_audit.artifacts.models import ReviewedSubmissionArtifact
from glite_english_audit.artifacts.submission import (
    SUBMISSION_SCHEMA_VERSION,
    SubmissionCounts,
    SubmissionPackage,
    compute_payload_hash,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)

_PLACEHOLDER_HASH = "0" * 64


class MaterializationError(Exception):
    """The reviewed artifact cannot become a valid submission package."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        details = "; ".join(f"{d.code}: {d.message}" for d in diagnostics)
        super().__init__(f"submission package failed its deterministic gate: {details}")
        self.diagnostics = diagnostics


def materialize_package(
    reviewed: ReviewedSubmissionArtifact,
    *,
    submission_id: str | None = None,
    recovery_secret: str | None = None,
) -> SubmissionPackage:
    """Build the exported package from the reviewed submission artifact.

    ``submission_id`` and ``recovery_secret`` are injectable for tests; a real
    run always generates fresh random values. The reviewed payload is frozen
    for idempotent delivery: changing the selected records afterwards must go
    through a new reviewed artifact, which produces a new payload ID.
    """
    included = [entry for entry in reviewed.records if entry.included]
    producer_versions = {entry.privacy_creator_version for entry in included}
    verifier_versions = {entry.privacy_verifier_version for entry in included}
    producer_version = max(producer_versions) if producer_versions else "0"
    privacy_verifier_version = max(verifier_versions) if verifier_versions else "0"

    unsigned = SubmissionPackage(
        submission_schema_version=SUBMISSION_SCHEMA_VERSION,
        submission_id=submission_id if submission_id is not None else new_submission_id(),
        recovery_secret=recovery_secret if recovery_secret is not None else new_recovery_secret(),
        payload_hash=_PLACEHOLDER_HASH,
        client_version=CLIENT_VERSION,
        producer_version=producer_version,
        privacy_verifier_version=privacy_verifier_version,
        records=[entry.record for entry in included],
        counts=SubmissionCounts.from_audit_counts(reviewed.counts),
    )
    package = unsigned.model_copy(update={"payload_hash": compute_payload_hash(unsigned)})

    diagnostics = verify_submission_package(package)
    diagnostics.extend(verify_package_against_review(package, reviewed))
    if diagnostics:
        raise MaterializationError(diagnostics)
    return package
