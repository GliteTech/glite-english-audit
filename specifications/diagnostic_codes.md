# Diagnostic codes

Every verifier, error surface, and server response in this project reports problems through
stable diagnostic codes. This document states the conventions and lists the current registry.

## Conventions

- Codes are stable and append-only. A released code keeps its meaning forever. Renaming or
  repurposing a code is a breaking contract change and is forbidden.
- Codes are `UPPER_SNAKE_CASE` strings grouped by prefix family:

| Family | Covers |
|---|---|
| `SCHEMA_` | JSON/JSONL parsing, required and forbidden fields, values, schema versions |
| `CARDINALITY_` / `ARITHMETIC_` | Line and record counts, arithmetic invariants |
| `LINEAGE_` | Input hashes, stale references, missing inputs |
| `PRIVACY_` | Forbidden patterns in content that must stay privacy-safe |
| `SUBMISSION_` | Submission allowlist, counts, payload hash |
| `SOURCE_` | Source discovery, accessibility, snapshot safety |
| `FINDING_` | Stage-4 findings checked against the strict non-native threshold |
| `STATE_` | Run state machine, checkpoints, resume |
| `SKILL_` | Canonical skill files and generated wrappers |
| `AUTHORSHIP_` | Stage-3 authorship decisions checked against their candidate text |

- Each code has a fixed severity: `error` blocks promotion of the artifact, `warning` is
  surfaced and recorded but does not block by itself, `info` is informational.
- Diagnostics are created only through `Diagnostic.from_code(...)`, which takes the severity from
  the registry. An unknown code raises immediately: it is a programming error, never
  data-dependent.

## Authoritative registry

`src/glite_english_audit/diagnostics/codes.py` is the single authoritative registry. Skills,
verifiers, tests, and the website contract reference these exact strings.
`tests/test_diagnostic_codes_doc.py` compares the table below against the registry row for
row — code, severity, and description — and fails on any difference, so this document cannot
drift.

### Adding a code

1. Append a `DiagnosticDefinition` to `_DEFINITIONS` in `codes.py` with a severity and a one-line
   description. Never edit or remove existing entries.
2. Pick the existing family whose prefix matches the failure class. A new family needs a new
   prefix row in the table above and a review of this document.
3. Update the code table below in the same change.
4. Descriptions state what was observed, not what to do about it, and must not name private
   values.

## Current code table

Generated from the registry; a sync test fails when this table and the registry disagree.

| Code | Severity | Description |
|---|---|---|
| `ARITHMETIC_INVARIANT_VIOLATION` | error | Counts fail a required arithmetic identity, such as shared plus withheld. |
| `AUTHORSHIP_DUPLICATE_DECISION` | error | More than one authorship decision covers the same candidate utterance. |
| `AUTHORSHIP_SPAN_NOT_VERBATIM` | error | A retained span is not an exact substring of its candidate's text. |
| `AUTHORSHIP_SPAN_ORDER_INVALID` | error | Retained spans overlap or do not follow their order in the candidate text. |
| `AUTHORSHIP_UNKNOWN_UTTERANCE` | error | A decision names an utterance that is not a candidate of this run. |
| `CARDINALITY_MISMATCH` | error | Line, record, or reference counts disagree with the declared cardinality. |
| `FINDING_CORRECTION_UNSUPPORTED` | error | A correction or explanation does not fix, or misdescribes, the problem. |
| `FINDING_EVIDENCE_MISMATCH` | error | A finding's original text does not appear in the cited utterance. |
| `FINDING_EXCLUDED_CATEGORY` | error | A finding targets an excluded category: slip, shorthand, style, or copied text. |
| `FINDING_MISSED_HIGH_CONFIDENCE` | warning | The unit contains a clear high-confidence mistake the producer did not report. |
| `FINDING_NATIVE_PLAUSIBLE` | error | A retained finding is plausible native informal English and must be dropped. |
| `LINEAGE_HASH_MISMATCH` | error | A recorded input hash does not match the current bytes of that input. |
| `LINEAGE_MISSING_INPUT` | error | A declared input artifact cannot be found in the run store. |
| `LINEAGE_STALE_REFERENCE` | error | An artifact references an artifact ID or hash the run manifest replaced. |
| `PRIVACY_CODE_PRESENT` | error | Source-code-shaped text appears in content that must stay plain English. |
| `PRIVACY_CONTEXT_DEPENDENT_RULE` | error | A rule sentence depends on hidden context, such as 'in this case' or 'here'. |
| `PRIVACY_CREDENTIAL_PATTERN` | error | A token, key, or secret-shaped string appears in checked content. |
| `PRIVACY_EMAIL_PRESENT` | error | An email address appears in content that must not contain one. |
| `PRIVACY_IDENTIFIER_PRESENT` | error | A UUID, hash, account number, or similar identifier appears in checked content. |
| `PRIVACY_INVISIBLE_CHARACTER` | error | Checked content changes under Unicode normalization, so what is displayed and what is stored differ. |
| `PRIVACY_LONG_SOURCE_PHRASE` | error | A verbatim example exceeds the allowed source-phrase length. |
| `PRIVACY_NAME_PRESENT` | error | A person, company, product, project, or place name appears in checked content. |
| `PRIVACY_PATH_PRESENT` | error | A file or directory path appears in content that must not contain one. |
| `PRIVACY_PHONE_PRESENT` | error | A phone-number-like sequence appears in content that must not contain one. |
| `PRIVACY_REIDENTIFICATION_RISK` | error | A combination of individually harmless facts could identify a person or company. |
| `PRIVACY_SUSPICIOUS_NUMBER` | error | An uncommon exact quantity, amount, or metric appears in checked content. |
| `PRIVACY_URL_PRESENT` | error | A URL or domain appears in content that must not contain one. |
| `SCHEMA_INVALID_JSON` | error | A file expected to contain JSON or JSONL could not be parsed. |
| `SCHEMA_INVALID_VALUE` | error | A field value fails validation against the artifact model. |
| `SCHEMA_MISSING_FIELD` | error | A required field is absent from a machine-readable artifact. |
| `SCHEMA_UNEXPECTED_FIELD` | error | An undeclared field is present in an artifact whose model forbids extras. |
| `SCHEMA_VERSION_UNSUPPORTED` | error | The artifact declares a schema version this code does not support. |
| `SKILL_EMPHASIS_BUDGET_EXCEEDED` | error | More than five emphasized MUST, NEVER, or CRITICAL rules appear in one file. |
| `SKILL_FRONTMATTER_INVALID` | error | SKILL.md frontmatter is missing, unparsable, or lacks name or description. |
| `SKILL_MISSING_FILE` | error | A canonical skill directory has no SKILL.md, or the file is empty. |
| `SKILL_NAME_MISMATCH` | error | Frontmatter name does not match the skill directory slug. |
| `SKILL_OUTPUT_FORMAT_MISSING` | warning | A skill that produces an artifact has no Output Format section. |
| `SKILL_REFERENCED_FILE_MISSING` | error | A local file referenced by a skill does not exist in the repository. |
| `SKILL_SECTION_MISSING` | error | A required section (Goal, Inputs, Context, Steps, Done When, Forbidden) is missing. |
| `SKILL_TITLE_COUNT` | error | The skill body does not contain exactly one top-level title. |
| `SKILL_VERSION_INVALID` | error | The skill body lacks a plain-integer **Version** marker. |
| `SKILL_WRAPPER_DRIFT` | error | A generated wrapper no longer matches its canonical skill. |
| `SKILL_WRAPPER_MISSING` | error | A generated .claude/skills or .codex/skills wrapper is missing. |
| `SOURCE_DISCOVERY_FAILED` | warning | One adapter failed during discovery; the remaining sources continued. |
| `SOURCE_INACCESSIBLE` | warning | Source data exists but cannot be read with current permissions. |
| `SOURCE_LOCKED` | warning | A source database is locked and no consistent snapshot could be taken. |
| `SOURCE_NOT_FOUND` | info | The source application or its data directory was not found. |
| `SOURCE_SNAPSHOT_NOT_IGNORED` | error | Git does not ignore the snapshot target, so snapshotting stopped. |
| `SOURCE_SNAPSHOT_SYNCED_ROOT` | error | The snapshot target sits in a cloud-synced or network root, so it was refused. |
| `SOURCE_SNAPSHOT_UNSAFE_PATH` | error | The snapshot target failed path-safety checks, so snapshotting stopped. |
| `SOURCE_UNSUPPORTED_SCHEMA` | warning | Source data was detected but its schema fingerprint is not supported. |
| `SOURCE_WSL_HOST_STORE_HINT` | info | A Windows-host data store was seen from WSL; run the audit from native Windows. |
| `STATE_CHECKPOINT_CORRUPT` | error | A checkpoint or manifest file is unreadable or fails validation. |
| `STATE_EXPIRED_INPUT` | warning | A private input required for resume passed the 30-day retention limit. |
| `STATE_INVALID_TRANSITION` | error | A run or stage attempted a transition the state machine forbids. |
| `STATE_RESUME_INCOMPATIBLE` | warning | A checkpoint fingerprint is incompatible with the current versions. |
| `STATE_RUN_DIRECTORY_MISMATCH` | error | A run directory name differs from the run ID recorded in its own manifest. |
| `STATE_RUN_ID_INVALID` | error | A run identifier does not match the required run- plus 32 hex digits form. |
| `STATE_UNSAFE_CLEANUP_PATH` | error | A retention cleanup target failed path-safety checks, so cleanup stopped. |
| `SUBMISSION_COUNT_MISMATCH` | error | Submission counts disagree with the reviewed submission artifact. |
| `SUBMISSION_FORBIDDEN_FIELD` | error | The submission package contains a field outside the allowlist. |
| `SUBMISSION_HASH_MISMATCH` | error | The canonical payload hash does not match the package contents. |
| `SUBMISSION_NO_RECORDS` | error | The package contains no detailed mistake record, so nothing can be sent. |

## Withheld reason codes

The registry also owns `WITHHELD_REASON_CODES`, the non-descriptive reason classes shared with
the submission contract. Glite learns how many records were withheld per class, never what the
mistakes were about:

- `WITHHELD_BY_USER`
- `WITHHELD_PRIVACY_UNSAFE`
- `WITHHELD_PROCESSING_FAILED`
