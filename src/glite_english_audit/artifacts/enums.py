"""Shared enumerations for steps, sources, and record classification.

Enum values are stable contract strings. Code compares enum members, never raw
strings; raw strings appear only at serialization boundaries.
"""

from enum import IntEnum, StrEnum


class StepId(IntEnum):
    """The five pipeline steps.

    One session is one file, and that file survives every step: after step a,
    each step reads the previous step's files and writes the same names back,
    so any step's output can be diffed against its input file by file. That
    property is the reason for the shape, and it is worth more than the nine
    finer-grained steps it replaced, which pooled every session into one
    JSONL and made "what did this step do to session X" unanswerable.

    Steps c, d and e are one agent per file, run in parallel. Steps a and b are
    deterministic code.

    Numbered for ordering — promotion checks compare ``int(step)`` — while the
    directory names lead with the letter the owner uses when talking about
    them. The review is not a step: it produces no per-session file and lives
    in the run's ``submission/`` directory, reflected in ``RunStatus.REVIEW``.
    """

    A_COLLECTED = 0
    B_DEDUPLICATED = 1
    C_AUTHORED = 2
    D_MISTAKES = 3
    E_VERIFIED = 4


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


class StepStatus(StrEnum):
    """Verification lifecycle of one step's current artifact."""

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
