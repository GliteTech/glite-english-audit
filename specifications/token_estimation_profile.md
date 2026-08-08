# Token estimation profile

Token and cost estimates come from measured calibration profiles, updated live during a run and
retained locally across runs. This document specifies the profile record, its partitioning keys,
the live recalibration inputs, the confidence rule, and where local history lives.

Only numbers are retained. No source text, benchmark summary, or source description ever enters a
profile or the local history.

## 1. Profile record

One record describes one measured stage/runtime/model/effort combination. Example (synthetic
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
| `step` | The semantic stage or skill step being measured. |
| `runtime` | Agent runtime that executed the step. |
| `model` | Pinned model ID. |
| `effort` | Model effort setting. |
| `messages_measured` | Sample size behind this record. |
| `average_words_per_message` | Mean eligible words per processed message. |
| `fixed_input_tokens_per_batch` | Prompt overhead paid once per batch. |
| `input_tokens_per_message` | Fresh input tokens per message. |
| `input_tokens_per_word` | Fresh input tokens per eligible word. |
| `cached_input_tokens_per_message` | Cached input tokens per message. |
| `output_tokens_per_message` | Output tokens per message. |
| `retry_rate` | Fraction of batches that needed a retry. |
| `p50_total_tokens_per_message` | Median total tokens per message. |
| `p90_total_tokens_per_message` | 90th-percentile total tokens per message. |

## 2. Partitioning keys

Measurements are comparable only inside a calibration cell. A cell is keyed by:

- Runtime.
- Pinned model ID.
- Effort.
- Stage (step).
- Skill version.
- Prompt version.
- Schema version.
- Batching strategy.

History from an incompatible cell receives little or no weight. After any version change, history
is partitioned; old cells are kept for reference but do not contaminate new estimates.

## 3. Live recalibration

After each completed batch, the estimate is updated from:

- Actual fresh, cached, and output tokens.
- Words and utterances completed.
- Stage.
- Model and effort.
- Batch size.
- Retry rate.
- Average artifact output size.
- Remaining stage work.

Updates use a robust weighted estimator so one unusual batch does not cause wild swings. The UI
shows actual usage so far, estimated remaining usage, estimated final total, a typical range, a
conservative upper bound, a confidence label, and budget or subscription-limit risk.

## 4. Confidence rule

A calibration cell is high-confidence only after at least 10 completed representative batches for
the exact runtime/model/effort/stage/prompt combination. Below that minimum, the UI labels the
estimate low-confidence and widens the displayed range. Estimates never promise that a remaining
subscription percentage guarantees completion.

## 5. Local history

Measurements persist across audits on the same machine, in the private runtime root
(`paths.calibration_history_path()`):

- macOS: `~/Library/Application Support/Glite English Audit/calibration/local-history.jsonl`
- Windows: `%LOCALAPPDATA%\Glite English Audit\calibration\local-history.jsonl`
- WSL and native Linux: `${XDG_STATE_HOME:-~/.local/state}/glite-english-audit/calibration/local-history.jsonl`

Native Windows and WSL keep separate histories. The file is JSONL, one numerical measurement
record per line, with owner-only permissions. It contains no text, no source instance, path,
project, session ID, timestamp of user content, example, or user identifier. Public adapter IDs
such as `claude_code`, `codex`, or `wispr_flow` may appear because source shape affects cost.

Users can clear local calibration history at any time; clearing only widens future estimates.

## 6. Development calibration

Pre-release calibration harnesses run under the Git-ignored `temp/token-calibration/` directory
and fail if that directory is tracked. They read real source data in place and retain only the
numerical coefficients described above, written to `calibration/token-usage-profile.json`.
