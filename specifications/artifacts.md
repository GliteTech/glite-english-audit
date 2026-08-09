# Artifacts, stages, and lineage

This document specifies the audit waterfall: the nine stages, the artifact each stage produces,
who produces and verifies it, the shared artifact envelope, serialization conventions, the
deterministic plain-findings format, and how replacement and invalidation work.

Authoritative code: `src/glite_english_audit/artifacts/`. Pydantic models are the definitions of
record for every project-owned machine-readable artifact. This document must stay in sync with
them; where prose and model disagree, fix the mismatch instead of coding around it.

## 1. Stages

Stage IDs are the `StageId` enum in `artifacts/enums.py`. Every stage has one current artifact
per run, a producer with a version, a deterministic verifier, and — where meaning is involved —
an independent semantic verifier.

| Stage | Name | Artifact | Producer | Verifier |
|---|---|---|---|---|
| 0 | `SOURCE_INVENTORY` | `SourceInventoryArtifact` (list of `SourceInstanceRecord`, private) | Adapter `discover()` via the `discover-english-sources` skill | Deterministic inventory verifier (schema, adapter IDs, aggregate-only agent output) |
| 1 | `SOURCE_SNAPSHOTS` | One `SnapshotManifest` per selected instance describing a foreign read-only snapshot | Adapter `snapshot()` scripts | Deterministic snapshot verifier (path safety, file hashes, Git-ignore checks) |
| 2 | `CANDIDATE_UTTERANCES` | JSONL of `NormalizedUtterance` plus `CandidateUtterancesManifest` | Adapter `extract()` scripts | Deterministic verifier plus adapter `verify()` structural checks |
| 3 | `ELIGIBLE_ENGLISH` | Authorship decisions JSONL, then the filtered corpus plus `EligibleCorpusManifest` | Pre-filter (`normalization/authorship.py`) narrows candidates; the `filter-authored-english` skill judges which spans the learner wrote; `pipeline/apply_authorship.py` builds the corpus | Span verifier (every retained span is a verbatim substring, in order, non-overlapping) plus the deterministic corpus verifier (tokenizer version, counts, dedup invariants) |
| 4 | `PLAIN_FINDINGS` | Human-readable Markdown findings files, one per input unit, each with a `FindingsArtifactMeta` sidecar | `analyze-english-text` skill | Deterministic format verifier plus the independent `verify-english-findings` semantic verifier |
| 5 | `PRIVATE_MISTAKES` | JSONL of `PrivateMistake` plus `PrivateMistakesManifest` | `create-mistakes-jsonl` skill | Deterministic verifier (spans, occurrence IDs, double-count detection) plus semantic verification |
| 6 | `SAFE_RECORDS` | `SafeRecordCandidate` records wrapping `SafeMistakeRecord` | `create-private-safe-mistakes` skill (privacy-independent creator) | Deterministic privacy scanner |
| 7 | `PRIVACY_APPROVED` | The approved subset of stage-6 candidates, promoted by verification metadata | Promotion of stage-6 records | Deterministic privacy scanner plus the independent `verify-mistake-confidentiality` semantic verifier |
| 8 | `REVIEWED_SUBMISSION` | `ReviewedSubmissionArtifact` (private) and the exported `SubmissionPackage` | Review page decisions materialized by the `prepare-glite-submission` skill | Deterministic materializer checks (allowlist, count arithmetic, payload hash) |

Stages 0-7 artifacts are private and never leave the local machine. Only the stage-8
`SubmissionPackage` may be exported, and only through the allowlist in
`specifications/submission_contract.md`.

Verification reports and promotion events are separate append-only metadata artifacts. Verifying
an artifact never mutates it.

## 2. Artifact envelope

Every non-trivial artifact carries an `ArtifactEnvelope`
(`src/glite_english_audit/artifacts/envelope.py`, `ENVELOPE_SCHEMA_VERSION = 1`). Machine-readable
artifacts embed it as an `envelope` field. Intentionally human-readable artifacts, such as plain
findings, carry it in a `<name>.meta.json` sidecar.

| Field | Type | Meaning |
|---|---|---|
| `schema_name` | `str` | Name of the artifact schema. |
| `schema_version` | `int >= 1` | Version of that schema. |
| `artifact_id` | `str` | Unique artifact ID (`art-<32 hex>`). |
| `run_id` | `str` | Owning run (`run-<32 hex>`). |
| `stage_id` | `StageId` | Waterfall stage 0-8. |
| `producer_name` | `str` | Producing script or skill. |
| `producer_version` | `str` | Producer semver string. |
| `model_id` | `str \| None` | Model that produced the content, when one was used. |
| `model_effort` | `str \| None` | Model effort setting, when applicable. |
| `input_artifact_ids` | `list[str]` | IDs of the input artifacts. |
| `input_hashes` | `dict[str, str]` | Input artifact ID to SHA-256 hex digest. |
| `created_at` | `datetime` | Timezone-aware UTC creation time. |

Nothing from the envelope may enter an exported submission package. Run ID, stage ID, input IDs
and hashes, model metadata, local artifact ID, and the creation timestamp all stay local.

## 3. JSON and JSONL conventions

- Encoding is UTF-8 everywhere. `ensure_ascii` is false: non-ASCII characters are written
  directly, not escaped.
- Every project-owned model sets `model_config = ConfigDict(extra="forbid")`. Undeclared fields
  are validation errors (`SCHEMA_UNEXPECTED_FIELD`), not silently ignored data.
- JSONL files contain one JSON object per line, no blank interior lines, and end with a single
  trailing newline. Each JSONL file is described by a manifest that records its line count and
  SHA-256 digest.
- Stored JSON files are pretty-printed (two-space indent) for human inspection; hashing never
  uses the stored formatting.
- Canonical hashing (`artifacts/hashing.py`): every hash is a SHA-256 hex digest over canonical
  JSON bytes — sorted keys, compact separators `(",", ":")`, `ensure_ascii=False`, UTF-8 encoded.
  This form is deliberately simple so a TypeScript implementation can reproduce it exactly.
- Writes are atomic (temp file, fsync, rename) with owner-only permissions: mode `0600` files and
  `0700` directories on POSIX (`artifacts/io.py`).
- Handwritten JSON Schemas are forbidden. The committed schemas in `schemas/` are generated by
  `python -m glite_english_audit.artifacts.schema_export`; CI runs it with `--check` and fails on
  drift.

## 4. Stage 4: deterministic plain-findings format

The stage-4 findings artifact is Markdown-flavored plain text, one file per input unit. It is
private, may contain source language, and is never submitted to Glite. The layout is
deterministic so a script can verify it without model judgment.

### 4.1 File layout

- Encoding UTF-8, LF line endings, exactly one trailing newline.
- Line 1 is the title: `# English findings`.
- Line 2 is blank. Line 3 is the threshold statement, exactly:

  ```text
  Threshold: this audit reports only constructions that strongly suggest non-native English. Slips, chat shorthand, and native-plausible informal usage are not reported.
  ```

- After the threshold statement and one blank line, the file contains either one `## Finding N`
  block per retained construction, or the empty-result sentence.

### 4.2 Finding blocks

Blocks are numbered `1, 2, 3, ...` in order of appearance, with no gaps. Each block is:

```markdown
## Finding 1

Original: I very like this approach.
Correction: I really like this approach.
Why: "Very" cannot modify a verb directly; "really" or "very much" is used instead.
```

Rules:

- `Original:`, `Correction:`, and `Why:` lines are required, in that order, one line each, with a
  single space after the colon.
- An optional fourth line `Uncertainty:` may follow `Why:` when the analyzer retained the finding
  but wants to note residual doubt.
- No other lines are allowed inside a block. Blocks are separated by one blank line.

### 4.3 Empty result

When no finding is retained, the file contains — after the threshold statement and one blank
line — exactly this sentence on its own line, and nothing else:

```text
No high-confidence mistakes were found.
```

### 4.4 Sidecar

Every findings file `<name>.md` has a sidecar `<name>.md.meta.json` validating as
`FindingsArtifactMeta` (`artifacts/models.py`):

| Field | Type | Meaning |
|---|---|---|
| `envelope` | `ArtifactEnvelope` | Standard envelope (Section 2). |
| `unit_id` | `str` | ID of the analyzed input unit. |
| `utterance_ids` | `list[str]` | Utterances covered by this unit. |
| `finding_count` | `int >= 0` | Number of `## Finding N` blocks in the body. |
| `no_mistakes_found` | `bool` | True only for the empty-result form. |
| `body_relative_path` | `str` | Findings file path relative to the stage directory. |
| `body_sha256` | `str` | SHA-256 hex digest of the exact body bytes. |

Invariants the deterministic verifier enforces: `no_mistakes_found` implies `finding_count == 0`;
`finding_count` equals the number of blocks in the body; `body_sha256` matches the file bytes.

## 4A. Stage 3: who decides which words are the learner's

Authorship is a judgment, so a model makes it; counting is arithmetic, so code does. Splitting
them this way keeps specification 5.6's requirement that the word count be deterministic while
letting the harder question be answered by something that can read.

The order is:

1. `normalization/authorship.py` removes only unambiguous machinery and bulk — fenced code, stack
   traces, log lines, structured payloads — so the project does not pay to send a five-thousand
   word lint dump to a model. It is biased toward keeping: anything arguable survives for the
   model to judge. It is not an authorship decision and must not be treated as one.
2. `pipeline/authorship_batches.py` writes the survivors as numbered candidate batches.
3. The `filter-authored-english` skill returns, for each utterance, a decision of `retain`,
   `partial`, or `exclude` plus the spans the learner wrote, verbatim and in original order.
4. `pipeline/apply_authorship.py` locates every returned span in the candidate text by a single
   forward scan, which enforces verbatim wording, original order, and non-overlap together. A
   span that is absent, reordered, or overlapping quarantines its decision rather than entering
   the corpus, so a paraphrase or an invented sentence cannot reach the word denominator.
   Surviving spans are joined, classified for language, deduplicated across sources, and counted
   with the versioned tokenizer.

`normalization/filter_corpus.py` remains as a fallback that applies the pre-filter alone, used in
tests and where no model judgment is available. It is documented as such: pasted material the
pre-filter cannot recognize survives into the denominator and depresses every reported rate.

Measured on one real corpus, the pre-filter alone kept 93.4% of the words it was given while the
model kept 52.8% — so the fallback path understates a learner's error rate by roughly a factor of
1.8 on heavily pasted sources.

## 5. Replacement and invalidation

There is no `superseded_artifact_id`, no revision chain, and no historical-content chain.

- The run manifest (`artifacts/manifest.py`, `RunManifest.stages`) points to exactly one current
  artifact per stage: `current_artifact_id` plus `current_artifact_hash`.
- A repair atomically replaces the current output of a stage. The manifest then points to the
  replacement.
- Replacing an output invalidates every downstream artifact derived from the previous hash.
  Invalidated stages move to `INVALIDATED` status and are rerun before submission. A downstream
  artifact that still references a replaced ID or hash fails verification with
  `LINEAGE_STALE_REFERENCE`.
- Obsolete artifacts containing user text are deleted, not retained as historical revisions.
- A content-free event log records artifact IDs, hashes, diagnostic codes, and replacement events
  for debugging and resumption. It never contains source text.
- The reviewed stage-8 payload is frozen for idempotent delivery. Changing the selected records
  before submission produces a new payload with a new submission ID.
