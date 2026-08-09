"""Record models for the five pipeline steps.

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
from glite_english_audit.artifacts.hashing import sha256_hex

_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Utterance IDs are quoted into prompts, into the sentinel lines of the
# untrusted-data block that fences source text, and into decision and repair
# files. Part of every ID comes from the source record's own identifier, which
# is data someone else wrote: a record whose ID carries a newline and a forged
# 'END UNTRUSTED SOURCE TEXT' line closes the fence early and puts the rest of
# itself outside it, where it reads as operator instruction. So the ID is held
# to one line of ordinary identifier characters.
_ID_CHARACTERS = r"A-Za-z0-9_.:-"
_UTTERANCE_ID_PATTERN = re.compile(rf"^[{_ID_CHARACTERS}]{{1,256}}$")
_ID_PART_PATTERN = re.compile(rf"^[{_ID_CHARACTERS}]{{1,128}}$")

# Instance keys become directory and file names: step 1 writes snapshots to
# '<snapshots>/<adapter_id>/<instance_key[:12]}' and its manifest to
# '<step>/<instance_key[:12]}.json'. A key holding a separator or a dot run
# puts both outside the run's own tree, where manifest-bounded cleanup will not
# reach the copied source data. No separators and no dots, so truncating one
# can never produce a traversal component.
_INSTANCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def safe_id_part(value: str) -> str:
    """Return ``value`` when it is a safe ID component, otherwise a digest of it.

    Adapters compose utterance IDs from source-provided identifiers. Passing an
    unsafe one through breaks the delimiter convention; dropping the record
    loses a real utterance, and raising loses the whole source instance to one
    poisoned record. A stable digest keeps the record addressable across reruns
    and carries nothing the source chose.
    """
    if _ID_PART_PATTERN.fullmatch(value):
        return value
    return "h" + sha256_hex(value.encode("utf-8"))[:16]


def _validate_utterance_id(value: str) -> str:
    if not _UTTERANCE_ID_PATTERN.fullmatch(value):
        msg = "utterance_id must be one line of identifier characters"
        raise ValueError(msg)
    return value


def _validate_instance_key(value: str) -> str:
    if not _INSTANCE_KEY_PATTERN.fullmatch(value):
        msg = f"instance_key must be a path-safe identifier: {value!r}"
        raise ValueError(msg)
    return value


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

    @field_validator("instance_key")
    @classmethod
    def _instance_key(cls, value: str) -> str:
        return _validate_instance_key(value)

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
    """Discovery output: every discovered instance, private form."""

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
        # Checked under both path flavors because Windows is a supported
        # platform: there a backslash separates components and a drive letter
        # or UNC prefix makes the path absolute, so '..\\..' escapes exactly
        # like '../..' does on POSIX.
        msg = f"snapshot entry path must be relative and contained: {value!r}"
        for flavor in (PurePosixPath, PureWindowsPath):
            pure = flavor(value)
            if pure.anchor or pure.drive or ".." in pure.parts or not pure.parts:
                raise ValueError(msg)
        return value


class SnapshotManifest(BaseModel):
    """Project-owned description of one foreign snapshot, taken during step a.

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

    @field_validator("instance_key")
    @classmethod
    def _instance_key(cls, value: str) -> str:
        return _validate_instance_key(value)


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

    @field_validator("utterance_id")
    @classmethod
    def _utterance_id(cls, value: str) -> str:
        return _validate_utterance_id(value)


class CandidateUtterancesManifest(BaseModel):
    """Manifest accompanying one step-a session file."""

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


class FindingsArtifactMeta(BaseModel):
    """Sidecar envelope for one human-readable step 4 findings file.

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
    """One verified structured mistake, in the shape the old pipeline used.

    Superseded by :class:`MistakeRecord`, which step d writes. Private.

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

    @field_validator("utterance_id")
    @classmethod
    def _utterance_id(cls, value: str) -> str:
        return _validate_utterance_id(value)

    @field_validator("modality")
    @classmethod
    def _known_modality(cls, value: Modality) -> Modality:
        if value is Modality.UNKNOWN:
            msg = "a verified mistake must carry a resolved modality, not 'unknown'"
            raise ValueError(msg)
        return value


class PrivateMistakesManifest(BaseModel):
    """Manifest accompanying the private-mistakes JSONL file."""

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

    # The wording above is frozen, "Stage 6" and all. This docstring is exported
    # as the `description` of SafeMistakeRecord in three committed JSON Schemas,
    # and `specifications/contract_versions.md` freezes those files by digest —
    # so rewording it is a submission-contract change requiring a new version
    # row. The step it names is now step d. Fix the word when the contract
    # version next moves for a real reason, not before.

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


class MistakeRecord(BaseModel):
    """One mistake as steps d and e carry it: shareable content plus its address.

    Two things separate it from :class:`PrivateMistake`, which it replaces.

    It is shareable when written. Step d emits the six fields of
    :class:`SafeMistakeRecord` with a synthetic example, so no later step has to
    turn a private record into a safe one and step e confirms rather than
    repairs. A privacy-scanner hit on a step-d file is therefore a defect in
    step d, not a filter.

    It carries no ``original_text``. The old record quoted the learner's words
    and a verifier compared the quote with the span it claimed; a fabricated
    pair that agreed with itself passed that check. Here the span alone
    addresses the step-c file the run keeps, and the quote is resolved from
    there, which makes an invented quote impossible rather than detectable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    utterance_id: str
    evidence_span: EvidenceSpan
    mistake: str
    rule: str
    example: str
    example_type: ExampleType
    source_type: str
    modality: Modality

    @field_validator("utterance_id")
    @classmethod
    def _utterance_id(cls, value: str) -> str:
        return _validate_utterance_id(value)

    @property
    def record_id(self) -> str:
        """Local identity, derived from the address rather than declared.

        Two records for one utterance may not cover overlapping spans, so the
        pair is unique within a run and identical across reruns — an ID a model
        chooses is neither. It stays on the machine: a submission carries
        :meth:`shareable` alone.
        """
        return f"{self.utterance_id}:{self.evidence_span.start}-{self.evidence_span.end}"

    def shareable(self) -> SafeMistakeRecord:
        """The six fields exactly as Glite would receive them."""
        return SafeMistakeRecord(
            mistake=self.mistake,
            rule=self.rule,
            example=self.example,
            example_type=self.example_type,
            source_type=self.source_type,
            modality=self.modality,
        )

    @model_validator(mode="after")
    def _shareable_on_arrival(self) -> "MistakeRecord":
        # Every rule SafeMistakeRecord enforces applies at step d rather than at
        # the submission boundary. A record that cannot become one is a defect
        # the producing agent fixes while it still has the session in hand.
        self.shareable()
        return self


class SafeRecordCandidate(BaseModel):
    """Private wrapper linking a safe record to its private mistake."""

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
    """Private review artifact: selected records plus review decisions.

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
