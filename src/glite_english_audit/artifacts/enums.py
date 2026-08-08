"""Shared enumerations for stages, sources, and record classification.

Enum values are stable contract strings. Code compares enum members, never raw
strings; raw strings appear only at serialization boundaries.
"""

from enum import IntEnum, StrEnum


class StageId(IntEnum):
    """The nine waterfall stages from the project specification, Section 5.1."""

    SOURCE_INVENTORY = 0
    SOURCE_SNAPSHOTS = 1
    CANDIDATE_UTTERANCES = 2
    ELIGIBLE_ENGLISH = 3
    PLAIN_FINDINGS = 4
    PRIVATE_MISTAKES = 5
    SAFE_RECORDS = 6
    PRIVACY_APPROVED = 7
    REVIEWED_SUBMISSION = 8


class AgentRuntime(StrEnum):
    """The agent runtime orchestrating the audit."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class OsEnvironment(StrEnum):
    """Tested operating-system environments. WSL is distinct from Linux."""

    MACOS = "macos"
    WINDOWS = "windows"
    WSL = "wsl"
    LINUX = "linux"


class Modality(StrEnum):
    """How the user produced the text, at extraction time."""

    WRITTEN = "written"
    SPOKEN_ASR = "spoken_asr"
    UNKNOWN = "unknown"


class TextStatus(StrEnum):
    """Whether the stored text is the user's raw production."""

    VERBATIM = "verbatim"
    CLEANED = "cleaned"
    UNKNOWN = "unknown"


class Stability(StrEnum):
    """Release maturity of an adapter or storage variant."""

    STABLE = "stable"
    BETA = "beta"
    EXPERIMENTAL = "experimental"


class Accessibility(StrEnum):
    """Discovery outcome for one source instance."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INACCESSIBLE = "inaccessible"


class ExampleType(StrEnum):
    """Provenance of the example inside a privacy-safe mistake record."""

    VERBATIM = "verbatim"
    REDACTED = "redacted"
    SYNTHETIC = "synthetic"


class StageStatus(StrEnum):
    """Verification lifecycle of one stage's current artifact."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PRODUCED = "produced"
    VERIFIED_DETERMINISTIC = "verified_deterministic"
    VERIFIED_SEMANTIC = "verified_semantic"
    PROMOTED = "promoted"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class RunStatus(StrEnum):
    """Lifecycle of one audit run."""

    CREATED = "created"
    SELECTING = "selecting"
    AWAITING_PREFLIGHT = "awaiting_preflight"
    PROCESSING = "processing"
    CHECKPOINTED = "checkpointed"
    BLOCKED = "blocked"
    REVIEW = "review"
    COMPLETED = "completed"
    COMPLETED_WITH_EXCLUSIONS = "completed_with_exclusions"
    EXPIRED = "expired"
