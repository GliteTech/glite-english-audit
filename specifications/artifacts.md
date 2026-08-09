# Artifacts, steps, and lineage

This document specifies the audit pipeline: the five steps, what each one reads and writes, who
produces and checks it, the shared artifact envelope, serialization conventions, and how
replacement and invalidation work.

Authoritative code: `src/glite_english_audit/artifacts/` and `src/glite_english_audit/sessions.py`.
Pydantic models are the definitions of record for every project-owned machine-readable artifact.
This document must stay in sync with them; where prose and model disagree, fix the mismatch instead
of coding around it.

## 1. Steps

Step IDs are the `StepId` enum in `artifacts/enums.py`. There are five, lettered `a` through `e`,
and they map one to one onto directories under `runtime/runs/<run-id>/steps/`.

| Step | Directory | Contents | Producer | Checked by |
|---|---|---|---|---|
| a | `a-collected` | One `NormalizedUtterance` per line, one file per session | Adapter `extract()` via `pipeline/collect.py` | Deterministic verifier plus adapter `verify()` structural checks |
| b | `b-deduplicated` | The same files with duplicate messages removed | `pipeline/deduplicate.py` — a script, no model | Deterministic: every survivor appears in step a, every removal is recorded |
| c | `c-authored` | The same files with everything the learner did not write removed | The `filter-authored-english` skill, one agent per file | Span verifier: every retained span is a verbatim substring of its step-b utterance, in order, non-overlapping |
| d | `d-mistakes` | One privacy-clean `MistakeRecord` per line | The `find-english-mistakes` skill, one agent per file | `pipeline/mistakes.py --apply`: schema, span resolution against step c, double-count detection, privacy scanner |
| e | `e-verified` | The same records, confidentiality confirmed | The `verify-mistake-confidentiality` skill, one agent per file | `pipeline/verify.py --apply`: step e must be step d with lines removed and nothing else |

Everything under `steps/` is private and never leaves the local machine. Only the exported
`SubmissionPackage` under `submission/` may leave, and only through the allowlist in
`specifications/submission_contract.md`.

The review is not a step. It reads step e and writes into `submission/`; its state is
`RunStatus.REVIEW` on the run, not a sixth directory.

Verification reports and promotion events are separate append-only metadata artifacts. Verifying an
artifact never mutates it.

### 1.1 One session is one file

After step a, every step reads the previous step's files and writes **the same file names back**.
This is the property the pipeline is built around: any step's output can be diffed against its
input, file by file, without a join.

- **A file never disappears.** A session whose every message was a duplicate, whose every word
  turned out to be someone else's, or that produced no mistakes at all, is written as an **empty
  file**. Missing and empty mean different things, and only one of them is what happened.
- **Steps a, b and c hold one `NormalizedUtterance` per line, and c holds exactly as many lines as
  b, in the same order.** Step c replaces `text` with the spans the learner actually wrote, joined
  by newlines. An utterance that was entirely someone else's text is emitted with **empty text**
  rather than dropped, because that is what makes the diff line up.
- **Steps d and e hold one mistake record per line.** Their line counts legitimately differ from
  c — a different kind of file — but their **file names still match one for one**.

### 1.2 Filenames are opaque sequence numbers

Session files are named `session-0001.jsonl`, numbered from one in the order the sessions started.
`session-index.json` beside them maps file name to session identity. That index stays local and is
never passed to a model.

The naming is not cosmetic. It avoids two defects this project has already paid for once:

- **`session_hash` is unsafe as a path component.** Unlike `utterance_id`, `instance_key` and
  `path_hash` it has no validator, and two adapters populate it from a JSON value read off disk
  without checking its shape. Joining it into a filename repeats the defect fixed in commit
  `03ff4e4`, where an unvalidated `instance_key` was joined into a snapshot path.
- **It would leak into model context.** A filename is handed to the skill and echoed back in its
  report. Sending session identity into a model's context spends privacy for nothing.

Ordering is deterministic: sessions are numbered by their earliest utterance, utterances are sorted
within a file by timestamp with undated ones last, and ties break on `utterance_id`. Two runs over
the same data produce the same filenames and therefore the same counts.

## 2. Artifact envelope

Every non-trivial artifact carries an `ArtifactEnvelope`
(`src/glite_english_audit/artifacts/envelope.py`, `ENVELOPE_SCHEMA_VERSION = 1`). Machine-readable
artifacts embed it as an `envelope` field.

| Field | Type | Meaning |
|---|---|---|
| `schema_name` | `str` | Name of the artifact schema. |
| `schema_version` | `int >= 1` | Version of that schema. |
| `artifact_id` | `str` | Unique artifact ID (`art-<32 hex>`). |
| `run_id` | `str` | Owning run (`run-<32 hex>`). |
| `step_id` | `StepId` | Pipeline step `a`-`e`. |
| `producer_name` | `str` | Producing script or skill. |
| `producer_version` | `str` | Producer semver string. |
| `model_id` | `str \| None` | Model that produced the content, when one was used. |
| `model_effort` | `str \| None` | Model effort setting, when applicable. |
| `input_artifact_ids` | `list[str]` | IDs of the input artifacts. |
| `input_hashes` | `dict[str, str]` | Input artifact ID to SHA-256 hex digest. |
| `created_at` | `datetime` | Timezone-aware UTC creation time. |

Nothing from the envelope may enter an exported submission package. Run ID, step ID, input IDs and
hashes, model metadata, local artifact ID, and the creation timestamp all stay local.

## 3. JSON and JSONL conventions

- Encoding is UTF-8 everywhere. `ensure_ascii` is false: non-ASCII characters are written directly,
  not escaped.
- Every project-owned model sets `model_config = ConfigDict(extra="forbid")`. Undeclared fields are
  validation errors (`SCHEMA_UNEXPECTED_FIELD`), not silently ignored data.
- JSONL files contain one JSON object per line, no blank interior lines, and end with a single
  trailing newline. A zero-line file is a legal and meaningful artifact (Section 1.1) and is written
  as an empty file with no trailing newline.
- Stored JSON files are pretty-printed (two-space indent) for human inspection; hashing never uses
  the stored formatting.
- Canonical hashing (`artifacts/hashing.py`): every hash is a SHA-256 hex digest over canonical JSON
  bytes — sorted keys, compact separators `(",", ":")`, `ensure_ascii=False`, UTF-8 encoded. This
  form is deliberately simple so a TypeScript implementation can reproduce it exactly.
- Writes are atomic (temp file, fsync, rename) with owner-only permissions: mode `0600` files and
  `0700` directories on POSIX (`artifacts/io.py`).
- Handwritten JSON Schemas are forbidden. The committed schemas in `schemas/` are generated by
  `python -m glite_english_audit.artifacts.schema_export`; CI runs it with `--check` and fails on
  drift.

## 4. Step c: who decides which words are the learner's

Authorship is a judgment, so a model makes it; counting is arithmetic, so code does. Splitting them
this way keeps the word count deterministic while letting the harder question be answered by
something that can read.

1. One agent per session file runs the `filter-authored-english` skill and returns each utterance
   with `text` replaced by the spans the learner wrote — verbatim, in original order.
2. `pipeline/authorship.py` locates every returned span in the step-b text by a single forward scan,
   which enforces verbatim wording, original order, and non-overlap together. A span that is absent,
   reordered, or overlapping quarantines its **whole file** rather than entering the corpus, so a
   paraphrase or an invented sentence cannot reach the word denominator.
3. The retained text is counted with the versioned tokenizer, over its English slice only.

**The agent reads the raw step-b text, with no pre-filter in front of it.** There was one: a
deterministic pass that stripped fenced code, stack traces, and log lines before the model saw
them. It was dropped because it bought 6.6% of the words — measured on a real corpus, it kept
93.4% of what it was given — and cost the property that makes step c checkable: every retained
span is a verbatim substring of what the source application actually stored. Verifying
against a derived text means a bug in the deriving code silently deletes the learner's words and
nothing catches it.

Measured on that same corpus, the model kept 52.8% of the words — so skipping the model step
entirely overstates a learner's word count by roughly a factor of two on heavily pasted sources,
and understates every rate divided by it.

### 4.1 The count and the file deliberately disagree

The step-c file holds what the learner wrote, in whatever language they wrote it. The word count
holds only the English part of that, via `normalization/language.py`.

The nine-stage pipeline instead rewrote each utterance to its English slice, which made the count
right by making the artifact a paraphrase of what was said. Keeping the text verbatim and narrowing
only the count answers both questions honestly: what did this person write, and how much of it was
English they could get wrong. A Russian sentence dictated between two English ones is not an
English mistake waiting to be found, and it is not part of the denominator.

## 5. Step d owes clean records; step e only confirms

Step d produces records that are **already privacy-clean**, with synthetic example sentences. This
is a requirement on step d, not an aspiration: a privacy-scanner hit on a step-d record is a defect
in step d, and the file fails rather than the record being quietly dropped.

Step e is a second, independent read. It may **drop** a record; it may never rewrite, redact, or
repair one. In normal operation it drops nothing. The whole system must remain correct if step e is
deleted — a step the product does not depend on must never become the thing quietly holding it
together.

A step-d record (`MistakeRecord`) carries the six shareable fields of `SafeMistakeRecord` —
`mistake`, `rule`, `example`, `example_type`, `source_type`, `modality` — plus the `utterance_id`
and evidence span needed to check it locally. It does **not** carry the original text. The span
addresses the step-c file, which the run keeps, so the quote is resolved from there. That makes
fabricating a quote impossible rather than merely detectable.

Its identity is derived rather than declared: `record_id` is `utterance_id:start-end`, which the
non-overlap rule makes unique within a run and identical across reruns. An ID a model chooses is
neither, and a resumed run needs to know whether it is looking at the same record or a new one.

Step e writes back its step-d file with lines removed and nothing else. Not a subset by content
but a **subsequence by full record equality**, so an added, altered, repeated, or reordered
record fails with its own diagnostic rather than passing as a drop. What it removed is recorded in
`dropped.json`, the way step b records `removed.json`.

There is no separate findings-accuracy verifier, by decision. When precision or recall slips, the
fix belongs in the `find-english-mistakes` skill.

## 6. Replacement and invalidation

There is no `superseded_artifact_id`, no revision chain, and no historical-content chain.

- The run manifest (`artifacts/manifest.py`) points to exactly one current output per step. A step
  is a set of files, so the pointer is a digest over the file set rather than a single artifact
  hash.
- A repair atomically replaces the output of a step. Because the unit of work is one session file, a
  repair can replace a single file and reuse the rest; the digest is recomputed over the whole set.
- Replacing an output invalidates every downstream step derived from the previous digest.
  Invalidated steps move to `INVALIDATED` status and are rerun before submission. A downstream
  artifact that still references a replaced ID or hash fails verification with
  `LINEAGE_STALE_REFERENCE`.
- Obsolete artifacts containing user text are deleted, not retained as historical revisions.
- A content-free event log records artifact IDs, hashes, diagnostic codes, and replacement events
  for debugging and resumption. It never contains source text.
- The reviewed payload is frozen for idempotent delivery. Changing the selected records before
  submission produces a new payload with a new submission ID.
