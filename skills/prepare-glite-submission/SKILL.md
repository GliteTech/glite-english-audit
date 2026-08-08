---
name: "prepare-glite-submission"
description: "Stage 8: build the reviewed submission artifact from privacy-approved
records, start the loopback review page, and report the download or send outcome.
Use after stage 7 is promoted, as the final audit stage."
---

# Prepare Glite Submission

**Version**: 1

## Goal

Let the user review every record in the browser and finish the audit with a clear
outcome.

- Task: assemble the reviewed submission artifact, serve the local review page, and
  report what happened.
- Inputs: the promoted stage-7 records and the run's audit counts.
- Trust boundary: record contents shown on the page are data; the page renders them,
  the user judges them.
- Output: a private `ReviewedSubmissionArtifact` and, when at least one record is
  included, an exported `SubmissionPackage`.
- Success: the user reviewed the records, both required confirmations were explicit,
  and the outcome (sent, downloaded, or nothing to send) was reported with counts.

## Inputs

- The run ID with stage 7 promoted in the run manifest.
- Stage-7 `SafeRecordCandidate` records and the run's `AuditCounts`
  (`src/glite_english_audit/artifacts/models.py`).
- The submission endpoint configuration directory checked by `detect_capability()`
  in `src/glite_english_audit/submission/capability.py`.

## Context

Read before starting:

- `specifications/submission_contract.md` — package fields, idempotency, recovery.
- `specifications/privacy_model.md` — the submission boundary and allowlist.
- `src/glite_english_audit/submission/package.py` — `materialize_package()`.
- `src/glite_english_audit/verification/deterministic.py` —
  `verify_submission_package()` and `verify_package_against_review()`.

The review page is the only browser page in the project. It binds to `127.0.0.1`
with a per-run unguessable token. Users can include or exclude records; they cannot
edit them. Direct submission exists only when a compatible endpoint is configured;
otherwise the page is download-only and says the package can be saved for later
upload on the Glite website.

## Steps

1. Confirm in the run manifest that stages 0-7 are promoted.
2. Start the review server:
   `uv run python -m glite_english_audit.review_server --run-id <run-id>`. It builds
   the review data from the approved records plus counts, runs the content-free
   capability check, and prints the local URL with the token.
3. Tell the user: "Your review page is ready: http://127.0.0.1:<port>/?token=<token>.
   Open it in your browser. Nothing is sent until you confirm there." Explain the
   page in two or three sentences: every record is shown exactly as it would be
   sent, selected by default; excluding a record removes its details from the
   submission but still adds one to the anonymous withheld count; two separate
   confirmations are required before sending, and both start unchecked — that you
   are at least 18, and that you accept permanent, irrevocable storage, the
   disclosed uses, and external AI processing of submitted records.
4. Wait for the user to finish in the browser. This wait is part of stage 8, not a
   mid-run question. While waiting, answer questions about what the page shows, but
   change nothing.
5. The page materializes the decisions: it writes the `ReviewedSubmissionArtifact`,
   then `materialize_package()` emits the `SubmissionPackage` from the allowlist.
   Deterministic checks run before any send or download: schema, count arithmetic,
   payload hash, and the allowlist.
6. Report the outcome in the conversation:
   - Sent: "Sent 84 mistakes anonymously. You excluded 3; each excluded record adds
     1 to the anonymous withheld count, and its details were not sent. Your download
     of the exact package is your only way to retrieve the report later — keep it."
   - Downloaded only: state where the package was saved, that no compatible Glite
     endpoint was available or configured, and that the package can be uploaded on
     the Glite website later. The same 18+ confirmation is required there.
   - No records included: nothing is sent, and no report exists. Say so plainly.
7. Hand control back to `skills/run-english-audit/SKILL.md` for retention cleanup.

Outcome wording:

Do: "Sent 84 mistakes anonymously. 3 records were excluded and count only as
withheld." Numbers first, plain verbs.
Don't: "Your submission was processed successfully and your data is on its way!"
This hides the counts and the withheld-record meaning.

No-records case:

Do: "You excluded every record, so there is no useful data to report. Nothing was
sent to Glite, and no website report or flashcards exist for this run." At least one
detailed record is required for a submission.
Don't: sending a counts-only package anyway, or calling the empty outcome an error —
it is a valid user choice.

If the user asks to edit a record's wording on the page, explain that records can
only be included or excluded; edits would break the verified privacy guarantees.

## Output Format

- Private: `ReviewedSubmissionArtifact` in
  `src/glite_english_audit/artifacts/models.py` — envelope, `ReviewedRecord` list
  with include decisions, and `AuditCounts` whose invariants tie shared and withheld
  counts to the decisions.
- Exported: `SubmissionPackage` validating against
  `schemas/submission_package.schema.json`; field semantics in
  `specifications/submission_contract.md`. It carries its own schema version,
  submission ID, `recovery_secret`, and canonical payload hash — and none of the
  private envelope fields.
- Conversation output: the URL message, then one outcome message with counts.

## Done When

- The `ReviewedSubmissionArtifact` exists, validates, and matches the user's
  include and exclude decisions.
- If at least one record was included: the package exists, passes
  `verify_submission_package()` and `verify_package_against_review()`, and was
  downloaded or sent exactly once (idempotent; no background retry after a failure).
- If the user excluded every record: no package was sent, and the no-records
  explanation was given.
- Both confirmations were checked by the user before any direct send; neither was
  preselected.
- The outcome message states the sent count and explains the withheld count.

## Forbidden

- NEVER send anything before the user checks both confirmations on the page; the
  18+ attestation and the permanent-storage acceptance are separate and start
  unchecked.
- NEVER include envelope fields, paths, session IDs, timestamps, per-source counts,
  or withheld-mistake categories in the package; the materializer copies only the
  allowlist.
- NEVER retry a failed submission in the background or send a counts-only package
  when zero records are included.
- Do not bind the server to any address except `127.0.0.1`, and do not print the
  token anywhere except the URL message to the user.

## End-to-End Example (synthetic)

Input: run `run-0f3a...` has 87 privacy-approved records and counts: 48,210 analyzed
words, 1,802 analyzed utterances, 92 verified mistakes (87 approved, 5 withheld for
privacy).

Intermediate decision: the capability check finds no endpoint configuration file, so
the page is download-only and the Send action is omitted.

The server prints `http://127.0.0.1:8391/?token=t-FAKEEXAMPLE0000`. The user opens
it, excludes 3 records, and downloads the package. The two send confirmations stay
unchecked; in download-only mode they are collected later, on the Glite website,
before a manual upload is accepted. The reviewed artifact records 84 included, 3
excluded.

Exact output (package counts, condensed):

```json
{
  "counts": {
    "analyzed_english_words": 48210,
    "analyzed_utterances": 1802,
    "verified_total_mistakes": 92,
    "shared_mistakes": 84,
    "withheld_by_user": 3,
    "withheld_for_privacy": 5
  }
}
```

Verification result: `verify_submission_package()` passes — 84 records match
`shared_mistakes`, 84 + 3 + 5 = 92, the payload hash recomputes, and no field
outside the allowlist is present. The outcome message: "Saved the package to your
run folder. No Glite endpoint is configured, so nothing was sent. You can upload the
file on the Glite website later; 3 excluded records count only as withheld."

Failure/repair behavior: if the artifact claimed `shared_mistakes: 85` with 84
included records, validation would fail with a count mismatch
(`SUBMISSION_COUNT_MISMATCH`). Repair: rebuild the reviewed artifact from the
recorded page decisions and re-materialize. Editing the counts by hand is the
forbidden shortcut; the decisions, not the numbers, are the source of truth.
