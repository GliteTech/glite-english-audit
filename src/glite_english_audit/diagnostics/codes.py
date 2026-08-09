"""Stable diagnostic codes used by every verifier and error surface.

Codes are append-only: a released code keeps its meaning forever. Verifiers,
skills, tests, and the website contract reference these exact strings, so a
rename is a breaking contract change. New codes are added to the registry with
a severity and a one-line description.
"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Severity(StrEnum):
    """How a diagnostic affects promotion of the artifact that produced it."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class DiagnosticDefinition:
    """Registry entry describing one stable diagnostic code."""

    code: str
    severity: Severity
    description: str


_DEFINITIONS: tuple[DiagnosticDefinition, ...] = (
    # Schema and format checks.
    DiagnosticDefinition(
        code="SCHEMA_INVALID_JSON",
        severity=Severity.ERROR,
        description="A file expected to contain JSON or JSONL could not be parsed.",
    ),
    DiagnosticDefinition(
        code="SCHEMA_MISSING_FIELD",
        severity=Severity.ERROR,
        description="A required field is absent from a machine-readable artifact.",
    ),
    DiagnosticDefinition(
        code="SCHEMA_UNEXPECTED_FIELD",
        severity=Severity.ERROR,
        description="An undeclared field is present in an artifact whose model forbids extras.",
    ),
    DiagnosticDefinition(
        code="SCHEMA_INVALID_VALUE",
        severity=Severity.ERROR,
        description="A field value fails validation against the artifact model.",
    ),
    DiagnosticDefinition(
        code="SCHEMA_VERSION_UNSUPPORTED",
        severity=Severity.ERROR,
        description="The artifact declares a schema version this code does not support.",
    ),
    # Cardinality and arithmetic invariants.
    DiagnosticDefinition(
        code="CARDINALITY_MISMATCH",
        severity=Severity.ERROR,
        description="Line, record, or reference counts disagree with the declared cardinality.",
    ),
    DiagnosticDefinition(
        code="ARITHMETIC_INVARIANT_VIOLATION",
        severity=Severity.ERROR,
        description="Counts fail a required arithmetic identity, such as shared plus withheld.",
    ),
    # Lineage.
    DiagnosticDefinition(
        code="LINEAGE_HASH_MISMATCH",
        severity=Severity.ERROR,
        description="A recorded input hash does not match the current bytes of that input.",
    ),
    DiagnosticDefinition(
        code="LINEAGE_STALE_REFERENCE",
        severity=Severity.ERROR,
        description="An artifact references an artifact ID or hash the run manifest replaced.",
    ),
    DiagnosticDefinition(
        code="LINEAGE_MISSING_INPUT",
        severity=Severity.ERROR,
        description="A declared input artifact cannot be found in the run store.",
    ),
    # Privacy pattern scanning.
    DiagnosticDefinition(
        code="PRIVACY_URL_PRESENT",
        severity=Severity.ERROR,
        description="A URL or domain appears in content that must not contain one.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_EMAIL_PRESENT",
        severity=Severity.ERROR,
        description="An email address appears in content that must not contain one.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_PHONE_PRESENT",
        severity=Severity.ERROR,
        description="A phone-number-like sequence appears in content that must not contain one.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_PATH_PRESENT",
        severity=Severity.ERROR,
        description="A file or directory path appears in content that must not contain one.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_CREDENTIAL_PATTERN",
        severity=Severity.ERROR,
        description="A token, key, or secret-shaped string appears in checked content.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_CODE_PRESENT",
        severity=Severity.ERROR,
        description="Source-code-shaped text appears in content that must stay plain English.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_IDENTIFIER_PRESENT",
        severity=Severity.ERROR,
        description=(
            "A UUID, hash, account number, or similar identifier appears in checked content."
        ),
    ),
    DiagnosticDefinition(
        code="PRIVACY_SUSPICIOUS_NUMBER",
        severity=Severity.ERROR,
        description="An uncommon exact quantity, amount, or metric appears in checked content.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_LONG_SOURCE_PHRASE",
        severity=Severity.ERROR,
        description="A verbatim example exceeds the allowed source-phrase length.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_CONTEXT_DEPENDENT_RULE",
        severity=Severity.ERROR,
        description="A rule sentence depends on hidden context, such as 'in this case' or 'here'.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_NAME_PRESENT",
        severity=Severity.ERROR,
        description=(
            "A person, company, product, project, or place name appears in checked content."
        ),
    ),
    DiagnosticDefinition(
        code="PRIVACY_REIDENTIFICATION_RISK",
        severity=Severity.ERROR,
        description=(
            "A combination of individually harmless facts could identify a person or company."
        ),
    ),
    # Submission allowlist.
    DiagnosticDefinition(
        code="SUBMISSION_FORBIDDEN_FIELD",
        severity=Severity.ERROR,
        description="The submission package contains a field outside the allowlist.",
    ),
    DiagnosticDefinition(
        code="SUBMISSION_COUNT_MISMATCH",
        severity=Severity.ERROR,
        description="Submission counts disagree with the reviewed submission artifact.",
    ),
    DiagnosticDefinition(
        code="SUBMISSION_HASH_MISMATCH",
        severity=Severity.ERROR,
        description="The canonical payload hash does not match the package contents.",
    ),
    DiagnosticDefinition(
        code="SUBMISSION_NO_RECORDS",
        severity=Severity.ERROR,
        description="The package contains no detailed mistake record, so nothing can be sent.",
    ),
    # Source discovery, snapshotting, and adapters.
    DiagnosticDefinition(
        code="SOURCE_NOT_FOUND",
        severity=Severity.INFO,
        description="The source application or its data directory was not found.",
    ),
    DiagnosticDefinition(
        code="SOURCE_INACCESSIBLE",
        severity=Severity.WARNING,
        description="Source data exists but cannot be read with current permissions.",
    ),
    DiagnosticDefinition(
        code="SOURCE_UNSUPPORTED_SCHEMA",
        severity=Severity.WARNING,
        description="Source data was detected but its schema fingerprint is not supported.",
    ),
    DiagnosticDefinition(
        code="SOURCE_LOCKED",
        severity=Severity.WARNING,
        description="A source database is locked and no consistent snapshot could be taken.",
    ),
    DiagnosticDefinition(
        code="SOURCE_SNAPSHOT_UNSAFE_PATH",
        severity=Severity.ERROR,
        description="The snapshot target failed path-safety checks, so snapshotting stopped.",
    ),
    DiagnosticDefinition(
        code="SOURCE_SNAPSHOT_NOT_IGNORED",
        severity=Severity.ERROR,
        description="Git does not ignore the snapshot target, so snapshotting stopped.",
    ),
    DiagnosticDefinition(
        code="SOURCE_SNAPSHOT_SYNCED_ROOT",
        severity=Severity.ERROR,
        description=(
            "The snapshot target sits in a cloud-synced or network root, so it was refused."
        ),
    ),
    DiagnosticDefinition(
        code="SOURCE_DISCOVERY_FAILED",
        severity=Severity.WARNING,
        description="One adapter failed during discovery; the remaining sources continued.",
    ),
    # Stage-4 semantic finding verification.
    DiagnosticDefinition(
        code="FINDING_NATIVE_PLAUSIBLE",
        severity=Severity.ERROR,
        description=(
            "A retained finding is plausible native informal English and must be dropped."
        ),
    ),
    DiagnosticDefinition(
        code="FINDING_EXCLUDED_CATEGORY",
        severity=Severity.ERROR,
        description=(
            "A finding targets an excluded category: slip, shorthand, style, or copied text."
        ),
    ),
    DiagnosticDefinition(
        code="FINDING_EVIDENCE_MISMATCH",
        severity=Severity.ERROR,
        description="A finding's original text does not appear in the cited utterance.",
    ),
    DiagnosticDefinition(
        code="FINDING_CORRECTION_UNSUPPORTED",
        severity=Severity.ERROR,
        description="A correction or explanation does not fix, or misdescribes, the problem.",
    ),
    DiagnosticDefinition(
        code="FINDING_MISSED_HIGH_CONFIDENCE",
        severity=Severity.WARNING,
        description=(
            "The unit contains a clear high-confidence mistake the producer did not report."
        ),
    ),
    # Run state, checkpoints, and resume.
    DiagnosticDefinition(
        code="STATE_INVALID_TRANSITION",
        severity=Severity.ERROR,
        description="A run or step attempted a transition the state machine forbids.",
    ),
    DiagnosticDefinition(
        code="STATE_RESUME_INCOMPATIBLE",
        severity=Severity.WARNING,
        description="A checkpoint fingerprint is incompatible with the current versions.",
    ),
    DiagnosticDefinition(
        code="STATE_CHECKPOINT_CORRUPT",
        severity=Severity.ERROR,
        description="A checkpoint or manifest file is unreadable or fails validation.",
    ),
    DiagnosticDefinition(
        code="STATE_EXPIRED_INPUT",
        severity=Severity.WARNING,
        description="A private input required for resume passed the 30-day retention limit.",
    ),
    DiagnosticDefinition(
        code="STATE_UNSAFE_CLEANUP_PATH",
        severity=Severity.ERROR,
        description="A retention cleanup target failed path-safety checks, so cleanup stopped.",
    ),
    DiagnosticDefinition(
        code="SOURCE_WSL_HOST_STORE_HINT",
        severity=Severity.INFO,
        description=(
            "A Windows-host data store was seen from WSL; run the audit from native Windows."
        ),
    ),
    # Skill verifier.
    DiagnosticDefinition(
        code="SKILL_MISSING_FILE",
        severity=Severity.ERROR,
        description="A canonical skill directory has no SKILL.md, or the file is empty.",
    ),
    DiagnosticDefinition(
        code="SKILL_FRONTMATTER_INVALID",
        severity=Severity.ERROR,
        description="SKILL.md frontmatter is missing, unparsable, or lacks name or description.",
    ),
    DiagnosticDefinition(
        code="SKILL_NAME_MISMATCH",
        severity=Severity.ERROR,
        description="Frontmatter name does not match the skill directory slug.",
    ),
    DiagnosticDefinition(
        code="SKILL_VERSION_INVALID",
        severity=Severity.ERROR,
        description="The skill body lacks a plain-integer **Version** marker.",
    ),
    DiagnosticDefinition(
        code="SKILL_TITLE_COUNT",
        severity=Severity.ERROR,
        description="The skill body does not contain exactly one top-level title.",
    ),
    DiagnosticDefinition(
        code="SKILL_SECTION_MISSING",
        severity=Severity.ERROR,
        description=(
            "A required section (Goal, Inputs, Context, Steps, Done When, Forbidden) is missing."
        ),
    ),
    DiagnosticDefinition(
        code="SKILL_OUTPUT_FORMAT_MISSING",
        severity=Severity.WARNING,
        description="A skill that produces an artifact has no Output Format section.",
    ),
    DiagnosticDefinition(
        code="SKILL_EMPHASIS_BUDGET_EXCEEDED",
        severity=Severity.ERROR,
        description="More than five emphasized MUST, NEVER, or CRITICAL rules appear in one file.",
    ),
    DiagnosticDefinition(
        code="SKILL_WRAPPER_MISSING",
        severity=Severity.ERROR,
        description="A generated .claude/skills or .codex/skills wrapper is missing.",
    ),
    DiagnosticDefinition(
        code="SKILL_WRAPPER_DRIFT",
        severity=Severity.ERROR,
        description="A generated wrapper no longer matches its canonical skill.",
    ),
    DiagnosticDefinition(
        code="SKILL_REFERENCED_FILE_MISSING",
        severity=Severity.ERROR,
        description="A local file referenced by a skill does not exist in the repository.",
    ),
    DiagnosticDefinition(
        code="STATE_RUN_ID_INVALID",
        severity=Severity.ERROR,
        description="A run identifier does not match the required run- plus 32 hex digits form.",
    ),
    DiagnosticDefinition(
        code="STATE_RUN_DIRECTORY_MISMATCH",
        severity=Severity.ERROR,
        description="A run directory name differs from the run ID recorded in its own manifest.",
    ),
    DiagnosticDefinition(
        code="PRIVACY_INVISIBLE_CHARACTER",
        severity=Severity.ERROR,
        description=(
            "Checked content changes under Unicode normalization, so what is displayed and "
            "what is stored differ."
        ),
    ),
    # Stage-3 authorship decisions checked against their candidate text.
    DiagnosticDefinition(
        code="AUTHORSHIP_UNKNOWN_UTTERANCE",
        severity=Severity.ERROR,
        description="A decision names an utterance that is not a candidate of this run.",
    ),
    DiagnosticDefinition(
        code="AUTHORSHIP_DUPLICATE_DECISION",
        severity=Severity.ERROR,
        description="More than one authorship decision covers the same candidate utterance.",
    ),
    DiagnosticDefinition(
        code="AUTHORSHIP_SPAN_NOT_VERBATIM",
        severity=Severity.ERROR,
        description="A retained span is not an exact substring of its candidate's text.",
    ),
    DiagnosticDefinition(
        code="AUTHORSHIP_SPAN_ORDER_INVALID",
        severity=Severity.ERROR,
        description="Retained spans overlap or do not follow their order in the candidate text.",
    ),
)

DIAGNOSTIC_DEFINITIONS: dict[str, DiagnosticDefinition] = {
    definition.code: definition for definition in _DEFINITIONS
}

# Withheld-mistake reason codes shared with the submission contract. They are
# deliberately non-descriptive: Glite learns how many records were withheld and
# the operational reason class, never what the mistakes were about.
WITHHELD_REASON_CODES: frozenset[str] = frozenset(
    {
        "WITHHELD_BY_USER",
        "WITHHELD_PRIVACY_UNSAFE",
        "WITHHELD_PROCESSING_FAILED",
    }
)


def definition_for(code: str) -> DiagnosticDefinition:
    """Return the registry definition for ``code``.

    Raises ``KeyError`` for unknown codes: an unknown code in runtime output is
    a programming error, never data-dependent.
    """
    return DIAGNOSTIC_DEFINITIONS[code]


class Diagnostic(BaseModel):
    """One structured verifier finding tied to a stable code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Severity
    message: str
    item_ref: str | None = None
    evidence_path: str | None = None

    @classmethod
    def from_code(
        cls,
        code: str,
        message: str,
        *,
        item_ref: str | None = None,
        evidence_path: str | None = None,
    ) -> "Diagnostic":
        """Build a diagnostic, taking the severity from the registry."""
        definition = definition_for(code)
        return cls(
            code=code,
            severity=definition.severity,
            message=message,
            item_ref=item_ref,
            evidence_path=evidence_path,
        )
