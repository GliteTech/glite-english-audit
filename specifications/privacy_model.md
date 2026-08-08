# Privacy model

The audit reads private coding-agent and dictation history on the user's machine. Only
privacy-safe mistake records and anonymous counts may ever leave it. This document specifies the
threat model, the safe-record rules, the submission boundary, and the retention rules.

## 1. Threat model

Raw source histories must be assumed to contain:

- Company and product names.
- Customer or employee identities.
- Financial, growth, conversion, or pricing numbers.
- Proprietary workflows and business plans.
- URLs, domains, email addresses, phone numbers, IDs, and file paths.
- Credentials and secrets.
- Source code and unique technical details.
- Personal, medical, legal, or relationship information.
- Combinations of harmless facts that identify a person or company.

Privacy checking must consider semantic re-identification, not only regex-detectable secrets. A
record with no secret in it can still identify a company from an unusual job title plus a metric
plus a product niche.

## 2. Safe-record rules

The safe-record creator (stage 6) must not include:

- Names of people, companies, products, clients, projects, repositories, or locations.
- Exact dates, amounts, percentages, user counts, prices, metrics, or uncommon quantities.
- URLs, domains, emails, phone numbers, IDs, paths, or code.
- Rare job titles or distinctive technical descriptions.
- Long source phrases.
- Context that reveals what the user or their organization is doing.
- A correction that restores private information omitted from the example.

Generic grammar words may be quoted. When any uncertainty remains, the example must be synthetic
(`example_type: "synthetic"`).

All examples below are synthetic.

### Do

```json
{
  "mistake": "Used 'informations' as a plural countable noun.",
  "rule": "The noun 'information' is uncountable in English and has no plural form.",
  "example": "Please send me these informations by tomorrow.",
  "example_type": "synthetic",
  "source_type": "claude_code",
  "modality": "written"
}
```

The example demonstrates the language problem with no names, numbers, or context. The rule stands
alone without hidden context.

### Don't

```json
{
  "mistake": "Used 'informations' as a plural countable noun.",
  "rule": "The word should be singular in this case.",
  "example": "Send the churn informations for Acme Corp Q3 (12.4%) to anna@example.com.",
  "example_type": "verbatim",
  "source_type": "claude_code",
  "modality": "written"
}
```

Three violations: the rule depends on hidden context ("in this case"), and the example leaks a
company name, a business metric, and an email address. The deterministic scanner rejects this
record with `PRIVACY_CONTEXT_DEPENDENT_RULE`, `PRIVACY_SUSPICIOUS_NUMBER`, and
`PRIVACY_EMAIL_PRESENT`.

### Don't

```json
{
  "mistake": "Wrong preposition after 'depends'.",
  "rule": "The verb 'depends' takes the preposition 'on', not 'from'.",
  "example": "Our migration off the legacy invoicing platform depends from the Berlin team's rollout script at /srv/deploy/run.sh.",
  "example_type": "redacted",
  "source_type": "codex",
  "modality": "written"
}
```

The rule is fine, but the example carries a path, a location, and enough workflow context to hint
at what the organization is doing. The safe version is a short synthetic sentence: "The result
depends from the input." with `example_type: "synthetic"`.

## 3. Submission boundary

The full field-level contract is in `specifications/submission_contract.md`. The privacy-relevant
summary:

The downloadable `SubmissionPackage` may contain only: the submission schema version, a random
idempotent submission ID, a random 256-bit recovery secret, the canonical payload hash, client
and producer and privacy-verifier versions, approved privacy-safe mistake records, and anonymous
counts (eligible and analyzed words and utterances, modality splits, verified total, shared,
withheld-by-user, withheld-for-privacy, and other withheld counts by non-descriptive reason
code).

It must not contain:

- Raw text or private findings.
- Any source application, adapter, or instance field outside the stable public `source_type`
  inside an approved mistake record.
- File or directory paths.
- Session IDs or timestamps.
- Workspace, repository, or destination application.
- Device or stable user identifiers.
- Per-source counts or groupings.
- Categories for withheld mistakes.

Consent is never embedded in the reusable package. A direct upload wraps the package in a
separate `NewSubmissionRequest` carrying only the consent booleans, the consent-policy version,
and the confirmation timestamp. A lookup resubmission (`ReportLookupRequest`) creates no new
learner data.

The materializer is allowlist-based: it copies only the named fields into the package. Any other
field is a bug, not a policy question, and fails verification with
`SUBMISSION_FORBIDDEN_FIELD`.

## 4. Creator independence and double protection

The privacy-safe record creator must know nothing about a later privacy audit. Its instructions
require a completely safe record on the first attempt; it must never rely on a downstream filter
to catch leaks.

Protection is nevertheless doubled:

1. Stage 6: the independent creator produces records under the safe-record rules above.
2. Stage 7: a deterministic scanner checks forbidden patterns (URLs, emails, phones, paths,
   credentials, code, identifiers, suspicious numbers, long source phrases, context-dependent
   rules), and an independent semantic confidentiality verifier — in a fresh context, without the
   creator's reasoning — checks semantic re-identification.

No record is submitted without both gates. The verifier reports structured diagnostics and never
silently repairs a record. Systemic privacy-stage failures pause the run. On top of both gates,
the user reviews every record on the local review page before anything is sent.

## 5. Retention

Private run state lives outside the Git checkout, with owner-only permissions (`0700`
directories, `0600` files on POSIX; a user-limited ACL on Windows):

- macOS: `~/Library/Application Support/Glite English Audit/`
- Windows: `%LOCALAPPDATA%\Glite English Audit\`
- WSL and native Linux: `${XDG_STATE_HOME:-~/.local/state}/glite-english-audit/`

Source snapshots are the one deliberate exception: they live only under
`<repository>/temp/runtime/<run-id>/snapshots/`, which is Git-ignored. Snapshot preflight
verifies path containment, symlink safety, Git-ignore status, and refuses cloud-synced or network
roots.

Retention is state-based:

- Snapshots are removed as soon as their verified downstream extraction no longer depends on
  them and no resume path requires them. Cleanup deletes only files listed in the snapshot's
  project-owned cleanup manifest; it never touches source application data.
- When an audit completes, the tool immediately deletes extracted source text,
  eligible-utterance corpora, private findings, private structured mistakes, sensitive
  diagnostics, and any remaining snapshots.
- A completed run keeps only the privacy-safe final package, non-sensitive completion and
  idempotency metadata, and numerical token-calibration history.
- An unfinished run keeps the private artifacts needed for exact resumption for 30 days after
  its last successful checkpoint. Older unfinished-run artifacts are deleted on the next launch
  or explicit cleanup, with the same manifest-bounded safeguards.
- Logs are structured and privacy-minimized: no raw utterances, no prompt or response bodies
  containing source text, no private examples, no source paths, no credentials.

V1 relies on normal operating-system account and disk security. It adds no application-level
encryption to resumable artifacts. The source applications already persist the underlying data,
but a second plaintext copy is still an additional exposure and must never be described as
risk-free.
