"""Stage record models for the audit waterfall.

These Pydantic models are the authoritative definitions for reading, writing,
and validating the project's internal JSON and JSONL (specification, Section
5.1). Every model forbids undeclared fields. Models that stay inside the
private runtime store may carry local provenance; anything exported to Glite
goes through the separate submission models, which strip it.
"""

import re
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glite_english_audit.artifacts.enums import (
    Accessibility,
    ExampleType,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.envelope import ArtifactEnvelope

_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# The stable public adapter IDs a submitted record may name (specification,
# 5.5). Frozen here rather than derived from the discovery registry on purpose:
# a submitted record must validate identically on a machine where no adapter is
# registered, and adding an adapter must be a deliberate contract change to the
# submission surface, not a side effect of registration.
PUBLIC_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        "aider",
        "claude_code",
        "cline",
        "codex",
        "cursor",
        "gemini_cli",
        "opencode",
        "roo_code",
        "wispr_flow",
    }
)


def _validate_adapter_id(value: str) -> str:
    if not _ADAPTER_ID_PATTERN.fullmatch(value):
        msg = f"not a valid public adapter ID: {value!r}"
        raise ValueError(msg)
    return value


class SourceInstanceRecord(BaseModel):
    """One discovered local source instance. Private: never shown to the model.

    The full record stays in the local run store. The agent conversation sees
    only the derived :class:`InstanceInventorySummary`, which carries an opaque
    label instead of any path or workspace metadata (specification, 2.4, 4.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: str
    adapter_version: str
    instance_key: str
    opaque_label: str
    storage_format: str
    schema_fingerprint: str
    path_hash: str
    os_environment: OsEnvironment
    app_version: str | None = None
    stability: Stability
    accessibility: Accessibility
    diagnostic_code: str | None = None
    estimated_records: int = Field(ge=0)
    earliest_timestamp: datetime | None = None
    latest_timestamp: datetime | None = None
    candidate_messages: int = Field(ge=0)
    candidate_words: int = Field(ge=0)
    candidate_bytes: int = Field(ge=0)

    @field_validator("adapter_id")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return _validate_adapter_id(value)

    @field_validator("path_hash")
    @classmethod
    def _path_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "path_hash must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value


class InstanceInventorySummary(BaseModel):
    """Agent-facing inventory row: opaque label plus aggregate numbers only.

    This is the only per-instance shape a discovery script may print for the
    agent. It must never gain a field that reveals a path, project, workspace,
    account, or any source text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: str
    adapter_version: str
    opaque_label: str
    stability: Stability
    accessibility: Accessibility
    diagnostic_code: str | None = None
    estimated_records: int = Field(ge=0)
    earliest_timestamp: datetime | None = None
    latest_timestamp: datetime | None = None
    candidate_messages: int = Field(ge=0)
    candidate_words: int = Field(ge=0)
    candidate_bytes: int = Field(ge=0)

    @field_validator("adapter_id")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return _validate_adapter_id(value)


class SourceInventoryArtifact(BaseModel):
    """Stage 0 output: every discovered instance, private form."""

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    instances: list[SourceInstanceRecord]


class SnapshotFileEntry(BaseModel):
    """One file captured into a snapshot, listed in the cleanup manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value

    @field_validator("relative_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        # Checked under both path flavours because Windows is a supported
        # platform: there a backslash separates components and a drive letter
        # or UNC prefix makes the path absolute, so '..\\..' escapes exactly
        # like '../..' does on POSIX.
        msg = f"snapshot entry path must be relative and contained: {value!r}"
        for flavour in (PurePosixPath, PureWindowsPath):
            pure = flavour(value)
            if pure.anchor or pure.drive or ".." in pure.parts or not pure.parts:
                raise ValueError(msg)
        return value


class SnapshotManifest(BaseModel):
    """Stage 1 record: project-owned description of one foreign snapshot.

    Cleanup may delete only files listed here, resolved under the run's
    snapshot directory (specification, 3.6).
    """

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    adapter_id: str
    instance_key: str
    snapshot_relative_dir: str
    files: list[SnapshotFileEntry]

    @field_validator("adapter_id")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return _validate_adapter_id(value)


class NormalizedUtterance(BaseModel):
    """One extracted candidate utterance (specification, 4.4). Private."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utterance_id: str
    source_adapter: str
    adapter_version: str
    session_hash: str
    timestamp: datetime | None = None
    text: str
    modality: Modality
    text_status: TextStatus
    authorship_confidence: float = Field(ge=0.0, le=1.0)
    authorship_basis: str
    source_path_hash: str
    destination_app: str | None = None
    content_flags: list[str] = Field(default_factory=list)

    @field_validator("source_adapter")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return _validate_adapter_id(value)


class CandidateUtterancesManifest(BaseModel):
    """Stage 2 manifest accompanying the candidate-utterance JSONL file."""

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    utterance_count: int = Field(ge=0)
    jsonl_relative_path: str
    jsonl_sha256: str

    @field_validator("jsonl_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "jsonl_sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value


class EligibleCorpusManifest(BaseModel):
    """Stage 3 manifest: eligible user-authored English after filtering."""

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    tokenizer_version: str
    utterance_count: int = Field(ge=0)
    english_word_count: int = Field(ge=0)
    quarantined_utterance_count: int = Field(ge=0)
    deduplicated_utterance_count: int = Field(ge=0)
    jsonl_relative_path: str
    jsonl_sha256: str

    @field_validator("jsonl_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "jsonl_sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value


class FindingsArtifactMeta(BaseModel):
    """Sidecar envelope for one human-readable stage 4 findings file.

    The findings body itself is Markdown-flavored plain text following the
    deterministic format in ``specifications/artifacts.md``. It is private and
    may contain source language; it is never submitted to Glite.
    """

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    unit_id: str
    utterance_ids: list[str]
    finding_count: int = Field(ge=0)
    no_mistakes_found: bool
    body_relative_path: str
    body_sha256: str

    @field_validator("body_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "body_sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _consistent_emptiness(self) -> "FindingsArtifactMeta":
        if self.no_mistakes_found and self.finding_count != 0:
            msg = "no_mistakes_found requires finding_count == 0"
            raise ValueError(msg)
        return self


class EvidenceSpan(BaseModel):
    """Half-open character span inside one utterance's text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "EvidenceSpan":
        if self.end <= self.start:
            msg = "evidence span end must be greater than start"
            raise ValueError(msg)
        return self


class PrivateMistake(BaseModel):
    """Stage 5 record: one verified structured mistake. Private.

    Occurrence-based and atomic: one record per verified occurrence, each with
    exactly one evidence span and one occurrence ID so verifiers can detect
    double counting (specification, 5.6).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mistake_id: str
    occurrence_id: str
    finding_artifact_id: str
    utterance_id: str
    evidence_span: EvidenceSpan
    original_text: str
    correction: str
    explanation: str
    modality: Modality
    source_adapter: str
    session_hash: str

    @field_validator("source_adapter")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return _validate_adapter_id(value)

    @field_validator("modality")
    @classmethod
    def _known_modality(cls, value: Modality) -> Modality:
        if value is Modality.UNKNOWN:
            msg = "a verified mistake must carry a resolved modality, not 'unknown'"
            raise ValueError(msg)
        return value


class PrivateMistakesManifest(BaseModel):
    """Stage 5 manifest accompanying the private-mistakes JSONL file."""

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    mistake_count: int = Field(ge=0)
    jsonl_relative_path: str
    jsonl_sha256: str

    @field_validator("jsonl_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            msg = "jsonl_sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value


class SafeMistakeRecord(BaseModel):
    """Stage 6 record: the only per-mistake shape Glite may ever receive.

    Exactly the six fields from specification 5.5. Content-level privacy rules
    (no names, numbers, URLs, context-dependent rules) are enforced by the
    creator skill and the deterministic scanner, not by this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mistake: str
    rule: str
    example: str
    example_type: ExampleType
    source_type: str
    modality: Modality

    @field_validator("source_type")
    @classmethod
    def _public_source_type(cls, value: str) -> str:
        # A shipped record may name only a stable public adapter ID. Any other
        # free-form string is a private label — a workspace, project, or client
        # name — and must never reach the wire (specification, 8.3).
        if value not in PUBLIC_SOURCE_TYPES:
            msg = "source_type must be one of the stable public adapter IDs"
            raise ValueError(msg)
        return value

    @field_validator("modality")
    @classmethod
    def _submission_modality(cls, value: Modality) -> Modality:
        if value is Modality.UNKNOWN:
            msg = "a submitted record must be 'written' or 'spoken_asr'"
            raise ValueError(msg)
        return value

    @field_validator("mistake", "rule", "example")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            msg = "record sentences must be non-empty"
            raise ValueError(msg)
        return value


class SafeRecordCandidate(BaseModel):
    """Stage 6/7 private wrapper linking a safe record to its private mistake."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mistake_id: str
    record: SafeMistakeRecord
    creator_version: str
    creation_failed: bool = False
    failure_reason_code: str | None = None

    @model_validator(mode="after")
    def _failure_shape(self) -> "SafeRecordCandidate":
        if self.creation_failed and self.failure_reason_code is None:
            msg = "a failed safe-record creation must carry a reason code"
            raise ValueError(msg)
        return self


class ModalityCounts(BaseModel):
    """Word and utterance counts for one modality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible_words: int = Field(ge=0)
    analyzed_words: int = Field(ge=0)
    eligible_utterances: int = Field(ge=0)
    analyzed_utterances: int = Field(ge=0)

    @model_validator(mode="after")
    def _analyzed_within_eligible(self) -> "ModalityCounts":
        if self.analyzed_words > self.eligible_words:
            msg = "analyzed words cannot exceed eligible words"
            raise ValueError(msg)
        if self.analyzed_utterances > self.eligible_utterances:
            msg = "analyzed utterances cannot exceed eligible utterances"
            raise ValueError(msg)
        return self


class AuditCounts(BaseModel):
    """The complete count set from specification 5.6.

    Arithmetic invariants live here so every consumer — the review page, the
    package materializer, and the deterministic verifier — sees the same rules.
    """

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
    def _non_negative(cls, value: dict[str, int]) -> dict[str, int]:
        for code, count in value.items():
            if count < 0:
                msg = f"withheld count for {code!r} is negative"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "AuditCounts":
        withheld_total = (
            self.withheld_by_user + self.withheld_for_privacy + sum(self.other_withheld.values())
        )
        if self.shared_mistakes + withheld_total != self.verified_total_mistakes:
            msg = (
                "verified_total_mistakes must equal shared_mistakes plus every withheld count: "
                f"{self.verified_total_mistakes} != {self.shared_mistakes} + {withheld_total}"
            )
            raise ValueError(msg)
        if self.analyzed_english_words > self.eligible_english_words:
            msg = "analyzed words cannot exceed eligible words"
            raise ValueError(msg)
        if self.analyzed_utterances > self.eligible_utterances:
            msg = "analyzed utterances cannot exceed eligible utterances"
            raise ValueError(msg)
        # 'written' and 'spoken_asr' partition the corpus: every eligible and
        # every analyzed unit belongs to exactly one of them. Anything less than
        # exact equality lets a modality overstate its own denominator, and the
        # website's per-1,000-word rate is wrong by that factor
        # (specification, 5.6).
        written, spoken = self.written, self.spoken_asr
        partitions: tuple[tuple[str, int, int], ...] = (
            (
                "eligible words",
                written.eligible_words + spoken.eligible_words,
                self.eligible_english_words,
            ),
            (
                "analyzed words",
                written.analyzed_words + spoken.analyzed_words,
                self.analyzed_english_words,
            ),
            (
                "eligible utterances",
                written.eligible_utterances + spoken.eligible_utterances,
                self.eligible_utterances,
            ),
            (
                "analyzed utterances",
                written.analyzed_utterances + spoken.analyzed_utterances,
                self.analyzed_utterances,
            ),
        )
        for label, modality_total, overall in partitions:
            if modality_total != overall:
                msg = (
                    f"modality {label} must sum to the overall count: {modality_total} != {overall}"
                )
                raise ValueError(msg)
        return self


class ReviewedRecord(BaseModel):
    """One record shown on the review page, with the user's include decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mistake_id: str
    record: SafeMistakeRecord
    included: bool
    privacy_creator_version: str
    privacy_verifier_version: str


class ReviewedSubmissionArtifact(BaseModel):
    """Stage 8 private artifact: selected records plus review decisions.

    Carries the normal private envelope. The exported package is derived from
    it by the materializer, which applies the Section 8.3 allowlist and drops
    every envelope field.
    """

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    records: list[ReviewedRecord]
    counts: AuditCounts

    @model_validator(mode="after")
    def _counts_match_records(self) -> "ReviewedSubmissionArtifact":
        included = sum(1 for record in self.records if record.included)
        excluded = len(self.records) - included
        if self.counts.shared_mistakes != included:
            msg = (
                f"shared_mistakes ({self.counts.shared_mistakes}) must equal the number of "
                f"included records ({included})"
            )
            raise ValueError(msg)
        if self.counts.withheld_by_user != excluded:
            msg = (
                f"withheld_by_user ({self.counts.withheld_by_user}) must equal the number of "
                f"user-excluded records ({excluded})"
            )
            raise ValueError(msg)
        return self
