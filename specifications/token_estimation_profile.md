# Token estimation profile

Token and cost estimates come from measured calibration profiles, updated live during a run and
retained locally across runs. This document specifies the profile record, its partitioning keys,
the live recalibration inputs, the confidence rule, and where local history lives.

Only numbers are retained. No source text, benchmark summary, or source description ever enters a
profile or the local history.

## 1. Profile record

One record describes one measured step/runtime/model/effort combination. Example (synthetic
numbers):

```json
{
  "step": "find-mistakes",
  "runtime": "claude-code",
  "model": "pinned-model-id",
  "effort": "medium",
  "messages_measured": 500,
  "average_words_per_message": 42,
  "fixed_input_tokens_per_batch": 1850,
  "input_tokens_per_message": 96,
  "input_tokens_per_word": 2.29,
  "cached_input_tokens_per_message": 80,
  "output_tokens_per_message": 31,
  "retry_rate": 0.04,
  "p50_total_tokens_per_message": 118,
  "p90_total_tokens_per_message": 176
}
```

Field meanings:

| Field | Meaning |
|---|---|
| `step` | The pipeline step or skill being measured. |
| `runtime` | Agent runtime that executed the step. |
| `model` | Pinned model ID. |
| `effort` | Model effort setting. |
| `messages_measured` | Sample size behind this record. |
| `average_words_per_message` | Mean eligible words per processed message. |
| `fixed_input_tokens_per_batch` | Prompt overhead paid once per model call. The name predates the five-step pipeline: a call was a batch of 25 messages and is now one session file. |
| `input_tokens_per_message` | Fresh input tokens per message. |
| `input_tokens_per_word` | Fresh input tokens per eligible word. |
| `cached_input_tokens_per_message` | Cached input tokens per message. |
| `output_tokens_per_message` | Output tokens per message. |
| `retry_rate` | Fraction of calls that needed a retry. |
| `p50_total_tokens_per_message` | Median total tokens per message. |
| `p90_total_tokens_per_message` | 90th-percentile total tokens per message. |

The unit these per-message fields count is the unit the step processes, which is not a message for
every step. See section 7.

### 1.1 Priced cells and retired cells

The profile has two lists. `entries` are the cells an estimate may use; `retired_entries` are cells
whose step the pipeline no longer runs. A retired cell is the same record it always was — the
measurement happened and deleting it would destroy a record — but a step that does not run must
never reach the total the user consents to at the preflight, so no estimator, model resolution, or
confidence calculation reads `retired_entries`.

Retired so far:

| Step | Why |
|---|---|
| `verify-findings` | Deleted. Problems with a finding are fixed in the skill that extracts mistakes. |
| `create-safe-records` | Merged into `find-mistakes`, which now owns privacy-clean output. |

Both were priced for some time after their steps stopped existing: together they charged 563 of the
2,533 units the estimator billed per 1,000 candidate messages, so 22% of the number the user
consented to described work nobody did.

## 2. Partitioning keys

Measurements are comparable only inside a calibration cell. A cell is keyed by:

- Runtime.
- Pinned model ID.
- Effort.
- Step.
- Skill version.
- Prompt version.
- Schema version.
- Work grouping: one session file per call.

History from an incompatible cell receives little or no weight. After any version change, history
is partitioned; old cells are kept for reference but do not contaminate new estimates.

## 3. Live recalibration

After each completed session, the estimate is updated from:

- Actual fresh, cached, and output tokens.
- Words and utterances completed.
- Step.
- Model and effort.
- Messages in the session.
- Retry rate.
- Average artifact output size.
- Remaining step work.

Updates use a robust weighted estimator so one unusual session does not cause wild swings. The UI
shows actual usage so far, estimated remaining usage, estimated final total, a typical range, a
conservative upper bound, a confidence label, and budget or subscription-limit risk.

## 4. Confidence rule

A calibration cell is high-confidence only after at least 10 completed representative samples for
the exact runtime/model/effort/step/prompt combination. A sample is one measured model call, and
the committed profile was measured at 25 messages per call, so the sample count is derived with
that number and not with the pipeline's current grouping — regrouping the work does not create
evidence. Below that minimum, the UI labels the estimate low-confidence and widens the displayed
range. Estimates never promise that a remaining
subscription percentage guarantees completion.

## 5. Local history

Measurements persist across audits on the same machine, in the private runtime root
(`paths.calibration_history_path()`):

- Every platform: `<repository>/runtime/calibration/local-history.jsonl`

The path is identical everywhere and sits inside the Git-ignored runtime tree. History therefore
belongs to a checkout rather than to the machine: a fresh clone starts with no calibration history
and rebuilds it from its own runs.

Native Windows and WSL keep separate histories whenever they use separate checkouts, which the
project specification (3.6) requires: a WSL clone lives on the WSL filesystem, so it cannot share
a history file with a native Windows clone. The file is JSONL, one numerical measurement
record per line, with owner-only permissions. It contains no text, no source instance, path,
project, session ID, timestamp of user content, example, or user identifier. Public adapter IDs
such as `claude_code`, `codex`, or `wispr_flow` may appear because source shape affects cost.

Users can clear local calibration history at any time; clearing only widens future estimates.

## 6. Development calibration

Pre-release calibration harnesses run under the Git-ignored `temp/token-calibration/` directory
and fail if that directory is tracked. They read real source data in place and retain only the
numerical coefficients described above, written to `calibration/token-usage-profile.json`.

## 7. What each committed cell was measured on

Every cell states its own unit and its own provenance, because they differ. A profile edit that
changes a number without changing this section is undocumented.

**A profile describes what was measured. It does not say what will run.** The per-file agents of
steps c, d and e inherit the model of the session that launches them; nothing in this product pins
a model, and nothing will. So the `model` and `effort` keys are provenance — they say who produced
these numbers — and the run may be on something else entirely. It usually is: the cells below were
measured on `claude-fable-5` at medium and `claude-opus-5` at xhigh, and a session runs whatever the
user's runtime is set to.

The difference is not academic. On one corpus of 101 flagged constructions, judged blind by two
independent judges at 90% inter-rater agreement, `claude-fable-5` reached 91% precision at 151,078
output tokens and a 799-second median, `claude-opus-5` 90% at 77,153 and 456 seconds, and
`claude-sonnet-5` 66% — a third of its flags were false positives — at 325,071 tokens and 1,023
seconds. Two of those three would be described badly by any one cell here.

What follows for code: `resolve_models` picks which cells an estimate is priced against and is
never what a user is told will run; the run manifest records
`runtime_session.observed_model_ids()`; and the estimate's `session` object reports the running
model beside the measured one so the preflight can say plainly when they differ.

| Step | Unit priced | Measured on |
|---|---|---|
| `judge-authorship` | One candidate utterance | Claude Code, `claude-fable-5`, medium, 198 utterances (2026-08-08/09) |
| `find-mistakes` | One retained utterance | Claude Code, `claude-fable-5`, medium, 250 messages in 10 batches of 25 (2026-08-08) |
| `confirm-confidentiality` | One session file | Claude Code, `claude-opus-5`, xhigh, 31 session agents (2026-08-09) |

### 7.1 The confidentiality cell

Source: one real end-to-end audit on 2026-08-09 — 31 sessions, 191 candidate messages, 5,369
English words after the authorship step, 84 mistake records. Step e ran one agent per session file,
including the sessions that held no record, and consumed 798,083 fresh input tokens, 2,417,478
cached input tokens, and 26,628 output tokens across those 31 agents. The committed cell carries
the fresh cost as `fixed_input_tokens_per_batch` (25,745 per call, one call per session file) and
the p50/p90 of per-session cached-plus-output as the per-unit totals. Its
`average_words_per_message` is the run's 173.2 authored words per session, nominal: both fresh
coefficients are 0.0, so no word count moves this cell.

The unit is the session file rather than the mistake record, and that is a finding of the run, not
a convenience. Of the 28 sessions whose record count the run reported, the 15 that held no record
at all cost a mean 106,982 tokens and the 13 that held one or more cost a mean 105,887; the session
holding 25 records cost less than the median empty one. At this record volume the cost tracks the
agent, not the records it judges, so pricing
per record would state a relationship the measurement contradicts. The 2.7 records an average
session held are inside the per-session number instead of multiplying it. The limit is stated
plainly: nothing in this sample shows where a denser corpus starts to cost more, so a corpus whose
sessions hold far more than 2.7 records needs a new measurement.

This is one run, on one machine, under one runtime and one model. `messages_measured` is therefore
31 sessions, which the section 4 rule converts to a single sample — far under the 10 needed for
high confidence — so the cell is reported low-confidence and its upper bound is widened. It is a
measurement, not a calibration.

## 8. What a full run measured against its own preflight

One run has been measured end to end against the numbers its preflight quoted.
It is the only sample of this size, so it is recorded rather than fitted to.

| | predicted | collected | ratio |
|---|---:|---:|---:|
| messages | 1,747 | 1,614 | 1.08x |
| words | 363,995 | 108,021 | **3.37x** |
| sessions | 250 | 160 | 1.56x |

**Messages interpolate; words do not.** A window's counts are scaled by the
share of an app's span that the window covers, which assumes both are spread
evenly through time. Messages nearly are. Words are not: a large old store's
average message is far longer than a recent one, and the interpolation inherits
the whole-store average. The overshoot concentrates in the largest instance.

**The session count was the fixable half.** `ASSUMED_MESSAGES_PER_SESSION` was
7, measured on a 20-session run. This run gives 10.09 across 160 sessions;
pooling both samples (1,755 messages, 180 sessions) gives 9.75, and the constant
is now 10. The call count drives every per-call fixed cost, and the
confidentiality step is mostly fixed cost, so the error reached the headline
figure: on this selection the p50 estimate falls from 152.1M tokens to 130.2M.

**The word figure does not reach the token estimate on Claude Code.**
`input_tokens_per_word` and `input_tokens_per_message` are 0.0 in all three
measured claude-code cells; those cells price per message from
`p50_total_tokens_per_message`. The Codex cells are `uncalibrated-default` and
do use per-word rates, so the same 3.37x would reach a Codex quota estimate
directly. That is the strongest argument for fixing the interpolation rather
than continuing to describe it.

**The real fix** is per-time-bucket word counts from each adapter probe, so a
window interpolates a volume instead of a duration. Until then the estimate says
so in its notes. A global words-per-message constant was considered and
rejected: it would replace a biased model with one user's number.

Not measurable yet: no run on disk has completed step d, so there is no observed
mistakes-per-word rate, and the retention constants (`AUTHORED_WORD_RETENTION`,
`AUTHORED_UTTERANCE_RETENTION`) have only a partial step-c sample — 24 of 160
sessions, 21% of utterances but 6.9% of words, which skews short. They are left
at their previous values.
