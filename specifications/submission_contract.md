# Submission contract

This document specifies the versioned contract between the local audit and the Glite website: the
downloadable `SubmissionPackage`, its request envelopes, the canonical payload hash, idempotency
and conflict semantics, and recovery-secret handling.

Authoritative code: `src/glite_english_audit/artifacts/submission.py`
(`SUBMISSION_SCHEMA_VERSION = 1`). The committed JSON Schemas in `schemas/` are generated from
these models; CI fails if they drift.

## 1. SubmissionPackage

The downloadable, resubmittable, allowlist-only package. It contains privacy-safe records and
anonymous counts, and nothing else. No envelope field, path, session ID, timestamp, or
per-source grouping may ever be added.

| Field | Type | Constraint |
|---|---|---|
| `submission_schema_version` | `int` | `>= 1` |
| `submission_id` | `str` | Matches `^sub-[0-9a-f]{32}$`; random and idempotent |
| `recovery_secret` | `str` | 64 lowercase hex characters (256 random bits) |
| `payload_hash` | `str` | SHA-256 hex digest; see Section 4 |
| `client_version` | `str` matching `^[0-9]+(\.[0-9]+)*$` | Local client version |
| `producer_version` | `str` matching `^[0-9]+(\.[0-9]+)*$` | Safe-record producer version |
| `privacy_verifier_version` | `str` matching `^[0-9]+(\.[0-9]+)*$` | Independent privacy-verifier version |
| `records` | `list[SafeMistakeRecord]` | Approved privacy-safe mistake records |
| `counts` | `SubmissionCounts` | Anonymous denominator and mistake counts |

Model invariant: `len(records) == counts.shared_mistakes`.

### SafeMistakeRecord

Exactly six fields (defined in `artifacts/models.py`): `mistake`, `rule`, `example` (non-empty
strings), `example_type` (`verbatim | redacted | synthetic`), `source_type` (one of the nine shipped public adapter IDs
such as `codex`, `claude_code`, `wispr_flow`), `modality` (`written | spoken_asr`; `unknown` is
rejected).

### SubmissionCounts

| Field | Type | Constraint |
|---|---|---|
| `eligible_english_words` | `int` | `>= 0` |
| `analyzed_english_words` | `int` | `>= 0` |
| `eligible_utterances` | `int` | `>= 0` |
| `analyzed_utterances` | `int` | `>= 0` |
| `written` | `ModalityCounts` | Word/utterance counts for written text |
| `spoken_asr` | `ModalityCounts` | Word/utterance counts for dictated text |
| `verified_total_mistakes` | `int` | `>= 0` |
| `shared_mistakes` | `int` | `>= 0` |
| `withheld_by_user` | `int` | `>= 0` |
| `withheld_for_privacy` | `int` | `>= 0` |
| `other_withheld` | `dict[str, int]` | Keys limited to `WITHHELD_REASON_CODES`; values `>= 0` |

`ModalityCounts` fields: `eligible_words`, `analyzed_words`, `eligible_utterances`,
`analyzed_utterances` (all `>= 0`; analyzed never exceeds eligible).

Arithmetic invariant, validated by the model and rechecked by the server:

```text
verified_total_mistakes == shared_mistakes + withheld_by_user + withheld_for_privacy + sum(other_withheld.values())
```

## 2. NewSubmissionRequest

Direct-upload envelope. Consent lives here, never inside the reusable package. For manual upload,
the website builds the same envelope after showing the current policy and receiving unchecked
affirmative confirmations.

| Field | Type | Constraint |
|---|---|---|
| `package` | `SubmissionPackage` | The exact package |
| `adult_attested` | `Literal[True]` | Must be `true` |
| `permanent_storage_and_uses_accepted` | `Literal[True]` | Must be `true` |
| `external_ai_processing_accepted` | `Literal[True]` | Must be `true` |
| `consent_policy_version` | `str` | The policy version shown to the user |
| `client_confirmation_at` | `datetime` | Timezone-aware confirmation time |

## 3. ReportLookupRequest

Resubmission of an existing package purely to retrieve its report. Creates no new learner data
and requires no new storage attestation.

| Field | Type |
|---|---|
| `package` | `SubmissionPackage` |

### Responses

`SubmissionAccepted`: `submission_id`, `state` (`received | processing | report_ready`),
`report_url` (nullable). `SubmissionRejected`: `diagnostic_codes` (list of safe diagnostic code
strings only; never content).

## 4. Canonical payload hash

`payload_hash` is computed as:

1. Dump the package to JSON-mode primitives, excluding the top-level `payload_hash` field itself.
2. Serialize as canonical JSON: sorted keys, compact separators `","` and `":"`,
   `ensure_ascii=False`, encoded as UTF-8 bytes.
3. `payload_hash = SHA-256 hex digest of those bytes` (lowercase).

Reference implementation: `compute_payload_hash()` / `verify_payload_hash()` in `submission.py`,
built on `model_canonical_hash()` in `artifacts/hashing.py`. The canonical form is deliberately
simple so the TypeScript website reproduces it byte for byte.

## 5. Idempotency and conflict semantics

- The server binds each `submission_id` to its canonical package hash on first acceptance.
- Same ID, valid secret, same canonical hash: the server returns the existing state or report.
  Repeated submission of the same valid package is an idempotent lookup, creates one server
  submission, and never duplicates data.
- Same ID, different content: the server returns a conflict and never modifies stored data.
- A failed network request must not trigger silent background retries; retrying is an explicit
  user action and remains idempotent.
- The reviewed payload is frozen before sending. Changing the selected records creates a new
  payload with a new `submission_id`.

## 6. Recovery-secret handling

- The secret is generated locally: 256 cryptographically random bits as 64 hex characters
  (`new_recovery_secret()` in `hashing.py`).
- The server derives a one-way hash of the secret, stores only that verifier, and discards the
  plaintext.
- A missing or wrong secret returns the same not-found response and timing class as an unknown
  submission ID. A caller cannot distinguish "wrong secret" from "no such submission."
- Recovery secrets and complete packages never appear in URLs, analytics, application logs, or
  error reports.

## 7. Version policy

- Contract changes are specification-first: change the Pydantic models and this document in the
  local repository, regenerate the schemas, and publish the versioned contract before any
  consumer adopts it.
- Generated JSON Schemas live in `schemas/` (`submission_package.schema.json`,
  `new_submission_request.schema.json`, `report_lookup_request.schema.json`,
  `submission_accepted.schema.json`, `submission_rejected.schema.json`). They are generated by
  `python -m glite_english_audit.artifacts.schema_export`; CI runs `--check` and fails on drift.
  Handwritten schemas are forbidden.
- The website repository pins and tests the contract versions it accepts. API and manual upload
  validate against the same schema and receive the same `untrusted_client` treatment.
- The local client enables direct submission only for an advertised compatible
  `submission_schema_version`. Unknown versions fail closed.
