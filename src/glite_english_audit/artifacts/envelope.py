"""The artifact envelope shared by every non-trivial private artifact.

The envelope records identity and lineage (specification, Section 5.2). It is
embedded in machine-readable artifacts as an ``envelope`` field, and written as
a sidecar ``<name>.meta.json`` for intentionally human-readable artifacts such
as plain findings. Verification reports and promotion events are separate
append-only metadata artifacts, so verifying an artifact never mutates it.

Nothing from the envelope may enter an exported submission package.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from glite_english_audit.artifacts.enums import StageId

ENVELOPE_SCHEMA_VERSION = 1


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


def as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime, reading naive input as UTC.

    A few sources store timezone-unknown local wall-clock time; Aider is the
    one shipped example. Their timestamps therefore reach the shared pipeline
    naive, and every comparison with an aware period bound, cutoff, or
    proximity window would raise ``TypeError``. Reading them as UTC is the
    single project-wide convention: it costs at most the machine's UTC offset
    in period precision and keeps ordering identical on every platform, which
    a local-time reading would not.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class ArtifactEnvelope(BaseModel):
    """Identity and lineage metadata for one artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: str
    schema_version: int = Field(ge=1)
    artifact_id: str
    run_id: str
    stage_id: StageId
    producer_name: str
    producer_version: str
    model_id: str | None = None
    model_effort: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "created_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @field_validator("input_hashes")
    @classmethod
    def _require_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        for artifact_id, digest in value.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                msg = f"input hash for {artifact_id!r} is not a SHA-256 hex digest"
                raise ValueError(msg)
        return value
