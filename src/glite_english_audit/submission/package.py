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
    VERSION_PATTERN,
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
_FALLBACK_VERSION = "0"


class MaterializationError(Exception):
    """The reviewed artifact cannot become a valid submission package."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        details = "; ".join(f"{d.code}: {d.message}" for d in diagnostics)
        super().__init__(f"submission package failed its deterministic gate: {details}")
        self.diagnostics = diagnostics


def _highest_version(versions: set[str], *, field_name: str) -> tuple[str, list[Diagnostic]]:
    """The highest declared version, or a diagnostic for any non-version string.

    The reviewed artifact carries versions asserted by the privacy skills, so
    they are validated here before they can be copied into the package. Ordering
    compares release components numerically: sorting the strings would rank
    '1.10.0' below '1.9.0'.
    """
    invalid = [value for value in versions if not VERSION_PATTERN.fullmatch(value)]
    if invalid:
        # The offending value is never echoed: it is exactly the kind of string
        # that carries a path, a session ID, or raw source text.
        return _FALLBACK_VERSION, [
            Diagnostic.from_code(
                "SUBMISSION_FORBIDDEN_FIELD",
                f"{len(invalid)} reviewed record version(s) are not plain version numbers",
                item_ref=field_name,
            )
        ]
    if not versions:
        return _FALLBACK_VERSION, []
    highest = max(versions, key=lambda value: tuple(int(part) for part in value.split(".")))
    return highest, []


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
    producer_version, producer_diagnostics = _highest_version(
        {entry.privacy_creator_version for entry in included},
        field_name="producer_version",
    )
    privacy_verifier_version, verifier_diagnostics = _highest_version(
        {entry.privacy_verifier_version for entry in included},
        field_name="privacy_verifier_version",
    )
    version_diagnostics = producer_diagnostics + verifier_diagnostics
    if version_diagnostics:
        raise MaterializationError(version_diagnostics)

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
