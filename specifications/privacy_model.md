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

The mistake-finding skill (step d) writes the record, and it must not include:

- Names of people, companies, products, clients, projects, repositories, or locations.
- Exact dates, amounts, percentages, user counts, prices, metrics, or uncommon quantities.
- URLs, domains, emails, phone numbers, IDs, paths, or code.
- Invisible or non-canonical characters. A zero-width space inside an address defeats every pattern check and is invisible to a human reviewer, so a record whose text changes under Unicode normalization is withheld (`PRIVACY_INVISIBLE_CHARACTER`) rather than rewritten.
- Rare job titles or distinctive technical descriptions.
- Long source phrases. An example runs to at most 15 words whatever its provenance, enforced by
  `PRIVACY_LONG_SOURCE_PHRASE`; quoting the learner is normal, quoting them at length is not.
- Context that reveals what the user or their organization is doing.
- A correction that restores private information omitted from the example.

Generic grammar words may be quoted.

### 2.1 Choosing the example

The example is the learner's own words wherever their own words are safe. Being shown an invented
sentence in place of the mistake they made costs the learner the evidence, and it is a cost worth
paying only when their words carry something that identifies them. Step d works down three rungs
and stops at the first that holds.

**1. `verbatim` — quote.** Take the shortest stretch of the learner's text that contains the
construction and reads as ordinary English on its own, trimmed at clause boundaries and at most 15
words, and copy it exactly. Spelling and punctuation come with it: an unrelated slip inside that
stretch travels with it, because that is what verbatim means. This rung holds when the stretch
carries nothing on the list above and no personal attribute — age, health, religion, family, first
language, nationality, or where the learner lives.

**2. `redacted` — substitute.** When the stretch is disqualified, replace each offending value with
a different concrete value and keep everything else the learner's. The replacement is:

- **The same grammatical kind.** Singular countable for singular countable, uncountable for
  uncountable, number for number. Swapping a bare noun for a noun phrase with an article silently
  repairs an article error and makes the record false about what the learner did.
- **Never a name for a name.** A person, company, product, client, project, repository, or place is
  replaced by an ordinary common noun, never by another name of the same kind. No step after step d
  can distinguish a substituted name from a real one — step e is given the published fields and
  nothing to compare them against — so a name that survives into a record is a name that ships, and
  the ban in the list above holds for substitutes exactly as it holds for quotes. When no common
  noun preserves the construction, the record falls to rung 3.
- **Unrelated, not adjacent.** For what remains substitutable — a language, a profession, a weekday,
  an ordinary object — one neighbouring language or one competitor in the same niche still narrows
  to the same guess.
- **An ordinary real thing, not a placeholder.** A common profession, an everyday object, another
  language. Not a bracketed slot, not a category word the reader can see was emptied.
- **Itself safe.** The substitute passes rung 1, so a replacement number is one or two digits with
  no decimal, percent, or currency sign.

The construction under examination is never substituted. It is the evidence.

**3. `synthetic` — invent.** When substituting every disqualifying value would leave a sentence that
no longer shows the problem, or whose remaining shape still says what the organization does, invent
a sentence demonstrating the same problem.

Three rules hold at every rung. No example contains a placeholder standing in for removed material:
no bracketed slots, no ellipses, no blanks — an example that has been hollowed out is not redacted,
it is a record that should have moved down a rung. The `mistake` and `rule` sentences never name a
value the example does not carry, because a record is one unit and scrubbing the example while the
rule restores the detail protects nothing. And when even an invented example cannot show the problem
without private context, the record is withheld rather than salvaged.

The learner sentences below are invented for this document. A record shown as `verbatim` illustrates
the shape of the rung, not text anyone wrote.

### Do — rung 1

```json
{
  "mistake": "Used 'explain me' without the preposition 'to'.",
  "rule": "The verb 'explain' takes an indirect object introduced by 'to': 'explain to me'.",
  "example": "Please explain me how this feature works.",
  "example_type": "verbatim",
  "source_type": "claude_code",
  "modality": "written"
}
```

The learner's own clause, unchanged. It names nobody, counts nothing, and would sit as comfortably
in one workplace as another, so quoting it discloses only that its author writes English this way —
which is the whole point of the record.

### Do — rung 2

The learner wrote "why Finnish is mentioned here?", which identifies nothing but pins their first
language. The language is not the error; the missing inversion is. Substituting an unrelated
language keeps the error intact and the attribute out:

```json
{
  "mistake": "Formed a direct question with statement word order.",
  "rule": "A direct wh-question puts the auxiliary before the subject: 'why is it mentioned'.",
  "example": "why Portuguese is mentioned here?",
  "example_type": "redacted",
  "source_type": "codex",
  "modality": "written"
}
```

The learner wrote "I live in Helsinki for 14 years." — a city and a duration, in a session that also
gives their age. The city is a place name, so it becomes a common noun rather than another city; the
duration becomes a different number:

```json
{
  "mistake": "Used the simple present for a state continuing over a stated period up to now.",
  "rule": "A state that began in the past and still holds takes the present perfect: 'I have lived'.",
  "example": "I live in the countryside for 7 years.",
  "example_type": "redacted",
  "source_type": "codex",
  "modality": "written"
}
```

A language stays a language and a city does not stay a city because the two carry different risks
downstream. "Portuguese" points at nobody and cannot be mistaken for a disclosure; a plausible city
name is indistinguishable from the learner's own, and the only thing standing between it and
publication is the claim that it was substituted.

### Do — rung 3

The learner wrote "Our migration off the legacy invoicing platform depends from the Berlin team's
rollout script at /srv/deploy/run.sh." Substituting the path, the city, and the platform would still
leave a sentence describing one organization's migration, so no substitution reaches safety and the
example is invented instead:

```json
{
  "mistake": "Used the preposition 'from' after the verb 'depends'.",
  "rule": "The verb 'depends' takes the preposition 'on', not 'from'.",
  "example": "The result depends from the input.",
  "example_type": "synthetic",
  "source_type": "codex",
  "modality": "written"
}
```

### Don't — a label is not a check

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
`PRIVACY_EMAIL_PRESENT`. Rung 1 disqualified this stretch before any of them; `verbatim` names where
the text came from and asserts nothing about whether it was allowed to travel.

### Don't — substitute something adjacent

```json
{
  "mistake": "Formed a direct question with statement word order.",
  "rule": "A direct wh-question puts the auxiliary before the subject.",
  "example": "why Estonian is mentioned here?",
  "example_type": "redacted",
  "source_type": "codex",
  "modality": "written"
}
```

The learner wrote "Finnish". Estonian is its closest relative, so a reader who knows that learns
almost exactly what the substitution was meant to hide. A substitute is chosen for having no
relationship to the original, not for being a different word.

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

## 3A. What reaches a model at all

Before either gate, there is the question of what a model is shown. An agent is handed a
**projection** of the step it works on — `src/glite_english_audit/pipeline/agent_io.py` — carrying
what the judgment needs and nothing that names anyone:

- steps c and d see an index, the text, and its modality. Not the session hash, not the source path
  hash, not the utterance ID, not the adapter version or the confidence the adapter established.
- step e sees the four content fields it judges. Not the utterance ID and not the evidence span:
  those are local addresses, and the skill used to instruct the agent not to judge them.

This is the same reasoning that keeps session identity out of filenames, applied to file contents.
It was briefly missing: the projection lived in the batching module and was deleted along with it,
after which every line handed to a step-c agent carried a 64-hex session hash and a 64-hex path
hash — 15% of everything the agents read.

An agent also writes only its decision. The artifacts are written by the drivers, so nothing a model
emits becomes a file a later step reads without a check in between.

## 4. Where the obligation sits, and why the second gate is not the mechanism

**Step d owes clean records.** Not "records the next step will clean" — clean records, on the
first attempt. The skill is written without reference to any later filter, because a producer that
knows something downstream will catch its leaks stops being careful about not producing them.

Protection is nevertheless doubled:

1. Step d: the skill produces records under the safe-record rules above, choosing each example's
   provenance by Section 2.1. A deterministic scanner runs over its output. A scanner hit here
   **fails the file** and is reported as a defect in step d, rather than the record being quietly
   dropped — otherwise the defect is invisible and the failure rate is never measured. This is why
   rung 1's disqualifiers are written as a superset of what the scanner matches: an agent that
   quotes to the edge of the pattern checks turns a routine record into a failed session file.
2. Step e: an independent semantic confidentiality verifier — in a fresh context, without step d's
   reasoning — checks semantic re-identification, which no pattern check can do. It may drop a
   record. It may never rewrite, redact, or repair one.

**The system must remain correct if step e is deleted.** That is the test for whether the
obligation is really sitting in step d. A step the product does not depend on must never become
the thing quietly holding it together, so step e dropping records regularly is a signal to fix
step d, not evidence that the design is working.

No record is submitted without both gates. The verifier reports structured diagnostics and never
silently repairs a record. Systemic privacy failures pause the run. On top of both gates, the user
reviews every record on the local review page before anything is sent.

## 5. Retention

Private run state lives in one place, with owner-only permissions (`0700` directories, `0600`
files on POSIX; a user-limited ACL on Windows):

- Every platform: `<repository>/runtime/`

Private state lives inside the checkout, in a tree the committed `.gitignore` excludes. One
location means one thing to inspect and one thing to delete: removing the checkout removes every
run, snapshot, and artifact with it, so no private data is left orphaned somewhere the user no
longer has a reason to look. Git ignoring the tree is a convention, not a permission boundary, so
snapshot creation still asks Git whether the path is genuinely ignored before writing source
copies into it.

Source snapshots are the one deliberate exception: they live only under
`<repository>/runtime/runs/<run-id>/snapshots/`, which is Git-ignored. Snapshot preflight
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
