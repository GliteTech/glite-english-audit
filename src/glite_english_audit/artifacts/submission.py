"""The versioned submission contract shared with the Glite website.

This module owns the canonical Pydantic models for the downloadable
``SubmissionPackage`` and the request envelopes (specification, Sections 8.3
and 11.1). The committed JSON Schemas in ``schemas/`` are generated from these
models; CI fails if they drift.

The package is the strict allowlist boundary: it contains privacy-safe records
and anonymous counts, and nothing else. No envelope field, path, session ID,
timestamp, or per-source grouping may ever be added here.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glite_english_audit.artifacts.hashing import model_canonical_hash
from glite_english_audit.artifacts.models import AuditCounts, ModalityCounts, SafeMistakeRecord
from glite_english_audit.diagnostics.codes import WITHHELD_REASON_CODES

SUBMISSION_SCHEMA_VERSION = 1

_RECOVERY_SECRET_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUBMISSION_ID_PATTERN = re.compile(r"^sub-[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SubmissionCounts(BaseModel):
    """Anonymous denominator and mistake counts inside the package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible_english_words: int = Field(ge=0)
    analyzed_english_words: int = Field(ge=0)
    eligible_utterances: int = Field(ge=0)
    analyzed_utterances: int = Field(ge=0)
    written: ModalityCounts
    spoken_asr: ModalityCounts
    verified_total_mistakes: int = Field(ge=0)
    shared_mistakes: int = Field(ge=0)
    withheld_by_user: int = Field(ge=0)
    withheld_for_privacy: int = Field(ge=0)
    other_withheld: dict[str, int] = Field(default_factory=dict)

    @field_validator("other_withheld")
    @classmethod
    def _known_reason_codes(cls, value: dict[str, int]) -> dict[str, int]:
        for code, count in value.items():
            if code not in WITHHELD_REASON_CODES:
                msg = f"unknown withheld reason code: {code!r}"
                raise ValueError(msg)
            if count < 0:
                msg = f"withheld count for {code!r} is negative"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "SubmissionCounts":
        withheld_total = (
            self.withheld_by_user + self.withheld_for_privacy + sum(self.other_withheld.values())
        )
        if self.shared_mistakes + withheld_total != self.verified_total_mistakes:
            msg = "verified_total_mistakes must equal shared plus withheld counts"
            raise ValueError(msg)
        return self

    @classmethod
    def from_audit_counts(cls, counts: AuditCounts) -> "SubmissionCounts":
        """Project the private count set onto the submission shape."""
        return cls(
            eligible_english_words=counts.eligible_english_words,
            analyzed_english_words=counts.analyzed_english_words,
            eligible_utterances=counts.eligible_utterances,
            analyzed_utterances=counts.analyzed_utterances,
            written=counts.written,
            spoken_asr=counts.spoken_asr,
            verified_total_mistakes=counts.verified_total_mistakes,
            shared_mistakes=counts.shared_mistakes,
            withheld_by_user=counts.withheld_by_user,
            withheld_for_privacy=counts.withheld_for_privacy,
            other_withheld=dict(counts.other_withheld),
        )


class SubmissionPackage(BaseModel):
    """The downloadable, resubmittable, allowlist-only package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_schema_version: int = Field(ge=1)
    submission_id: str
    recovery_secret: str
    payload_hash: str
    client_version: str
    producer_version: str
    privacy_verifier_version: str
    records: list[SafeMistakeRecord]
    counts: SubmissionCounts

    @field_validator("submission_id")
    @classmethod
    def _submission_id(cls, value: str) -> str:
        if not _SUBMISSION_ID_PATTERN.fullmatch(value):
            msg = "submission_id must look like 'sub-<32 hex>'"
            raise ValueError(msg)
        return value

    @field_validator("recovery_secret")
    @classmethod
    def _recovery_secret(cls, value: str) -> str:
        if not _RECOVERY_SECRET_PATTERN.fullmatch(value):
            msg = "recovery_secret must be 64 lowercase hex characters"
            raise ValueError(msg)
        return value

    @field_validator("payload_hash")
    @classmethod
    def _payload_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "payload_hash must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _records_match_counts(self) -> "SubmissionPackage":
        if len(self.records) != self.counts.shared_mistakes:
            msg = (
                f"package holds {len(self.records)} records but counts claim "
                f"{self.counts.shared_mistakes} shared mistakes"
            )
            raise ValueError(msg)
        return self


def compute_payload_hash(package: SubmissionPackage) -> str:
    """Canonical SHA-256 over every package field except ``payload_hash``."""
    return model_canonical_hash(package, exclude={"payload_hash"})


def verify_payload_hash(package: SubmissionPackage) -> bool:
    """True when the embedded payload hash matches the package contents."""
    return package.payload_hash == compute_payload_hash(package)


class NewSubmissionRequest(BaseModel):
    """Direct-upload envelope. Consent lives here, never inside the package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: SubmissionPackage
    adult_attested: Literal[True]
    permanent_storage_and_uses_accepted: Literal[True]
    external_ai_processing_accepted: Literal[True]
    consent_policy_version: str
    client_confirmation_at: datetime

    @field_validator("client_confirmation_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "client_confirmation_at must be timezone-aware"
            raise ValueError(msg)
        return value


class ReportLookupRequest(BaseModel):
    """Resubmission of an existing package purely to retrieve its report.

    Creates no new learner data and needs no new storage attestation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: SubmissionPackage


class SubmissionAccepted(BaseModel):
    """Server acknowledgment for an accepted or already-known submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str
    state: Literal["received", "processing", "report_ready"]
    report_url: str | None = None


class SubmissionRejected(BaseModel):
    """Server rejection carrying only safe diagnostic codes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostic_codes: list[str]
