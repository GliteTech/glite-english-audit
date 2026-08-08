"""Pure token and time estimators over calibration coefficients.

Every function here is deterministic over its inputs: profile coefficients
(specification, 10.1), private cross-run history records (10.3), and candidate
counts from local scripts (2.4). Estimates are ranges, never guarantees, and
low-confidence cells widen the upper bound (13.7).
"""

import math
from collections.abc import Iterable, Sequence
from enum import StrEnum
from statistics import median

from pydantic import BaseModel, ConfigDict, Field, model_validator

from glite_english_audit.estimation.profile import CalibrationRecord, TokenUsageProfileEntry

PRODUCER_VERSION: str = "0.1.0"

# Batch assumption: per-batch fixed overhead (skill text, schema, instructions)
# is amortized over batches of 25 messages, the batching strategy the profile
# coefficients were measured with. Callers pass a different size only when the
# orchestrator actually batches differently.
ASSUMED_BATCH_SIZE: int = 25

# Placeholder throughput pending real calibration: end-to-end audit throughput
# observed in early development runs falls roughly in this band. Both bounds
# are deliberately explicit constants so calibration can replace them in one
# place. Unit: total tokens per wall-clock minute.
THROUGHPUT_TOKENS_PER_MINUTE_LOW: int = 4000
THROUGHPUT_TOKENS_PER_MINUTE_HIGH: int = 9000

# A calibration cell is high-confidence only after at least this many
# compatible completed batches (specification, 13.7).
HIGH_CONFIDENCE_MIN_RECORDS: int = 10

# Low-confidence cells widen the conservative upper bound by this factor.
LOW_CONFIDENCE_UPPER_WIDENING: float = 1.5

# Robust live update: history records whose per-message tokens deviate from
# the median by more than this many median absolute deviations are discarded,
# so one unusual batch cannot swing the estimate (specification, 10.2).
MAD_CLAMP_MULTIPLIER: float = 3.0


class EstimateConfidence(StrEnum):
    """Confidence label for one estimate (specification, 13.7)."""

    HIGH = "high"
    LOW = "low"


class TokenEstimate(BaseModel):
    """A typical (p50) and conservative (p90) total-token estimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p50_tokens: int = Field(ge=0)
    p90_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _p90_at_least_p50(self) -> "TokenEstimate":
        if self.p90_tokens < self.p50_tokens:
            msg = "p90_tokens must be >= p50_tokens"
            raise ValueError(msg)
        return self


class TimeRange(BaseModel):
    """Estimated wall-clock processing time as a range in minutes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low_minutes: float = Field(ge=0)
    high_minutes: float = Field(ge=0)

    @model_validator(mode="after")
    def _high_at_least_low(self) -> "TimeRange":
        if self.high_minutes < self.low_minutes:
            msg = "high_minutes must be >= low_minutes"
            raise ValueError(msg)
        return self


class LiveEstimate(BaseModel):
    """Robust per-message token estimate recomputed from completed batches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p50_tokens_per_message: float = Field(ge=0)
    p90_tokens_per_message: float = Field(ge=0)
    records_used: int = Field(ge=0)
    records_discarded: int = Field(ge=0)


class PresetEstimate(BaseModel):
    """One row of the period-preset comparison table (specification, 2.4).

    ``words`` and ``time`` are ``None`` for rows that cannot be computed yet,
    such as custom dates before the user enters them. ``expected_use`` carries
    the last column verbatim: subscription percentage, price range, or a plain
    statement that quota or price is unavailable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    period: str
    words: int | None = Field(default=None, ge=0)
    time: TimeRange | None = None
    expected_use: str


def estimate_stage(
    words: int,
    utterances: int,
    entry: TokenUsageProfileEntry,
    *,
    batch_size: int = ASSUMED_BATCH_SIZE,
) -> TokenEstimate:
    """Estimate total tokens for one stage from per-message coefficients.

    Per-message p50/p90 covers input, cached input, and output at the
    calibrated average message length; the fixed per-batch overhead is added
    once per batch of ``batch_size`` messages. When the selected text is
    denser or sparser than the calibrated average, the input side is adjusted
    by ``input_tokens_per_word`` times the word delta.
    """
    if words < 0 or utterances < 0:
        msg = "words and utterances must be non-negative"
        raise ValueError(msg)
    if batch_size < 1:
        msg = "batch_size must be at least 1"
        raise ValueError(msg)
    if utterances == 0:
        return TokenEstimate(p50_tokens=0, p90_tokens=0)
    batches = math.ceil(utterances / batch_size)
    fixed_overhead = batches * entry.fixed_input_tokens_per_batch
    word_delta = words - utterances * entry.average_words_per_message
    word_adjustment = word_delta * entry.input_tokens_per_word
    p50 = utterances * entry.p50_total_tokens_per_message + fixed_overhead + word_adjustment
    p90 = utterances * entry.p90_total_tokens_per_message + fixed_overhead + word_adjustment
    p50_tokens = max(fixed_overhead, math.ceil(p50))
    p90_tokens = max(p50_tokens, math.ceil(p90))
    return TokenEstimate(p50_tokens=p50_tokens, p90_tokens=p90_tokens)


def estimate_run(stage_estimates: Iterable[TokenEstimate]) -> TokenEstimate:
    """Sum stage estimates into one run-level estimate."""
    p50_total = 0
    p90_total = 0
    for estimate in stage_estimates:
        p50_total += estimate.p50_tokens
        p90_total += estimate.p90_tokens
    return TokenEstimate(p50_tokens=p50_total, p90_tokens=p90_total)


def confidence_for(
    entry: TokenUsageProfileEntry, *, compatible_history_records: int
) -> EstimateConfidence:
    """Confidence of one (runtime, model, effort, step) cell.

    High-confidence requires at least :data:`HIGH_CONFIDENCE_MIN_RECORDS`
    compatible completed batches in the local history (specification, 13.7).
    An uncalibrated profile entry is always low-confidence.
    """
    if compatible_history_records < 0:
        msg = "compatible_history_records must be non-negative"
        raise ValueError(msg)
    if entry.is_uncalibrated and compatible_history_records < HIGH_CONFIDENCE_MIN_RECORDS:
        return EstimateConfidence.LOW
    if compatible_history_records >= HIGH_CONFIDENCE_MIN_RECORDS:
        return EstimateConfidence.HIGH
    return EstimateConfidence.LOW


def apply_confidence(estimate: TokenEstimate, confidence: EstimateConfidence) -> TokenEstimate:
    """Widen the conservative upper bound of a low-confidence estimate."""
    if confidence is EstimateConfidence.HIGH:
        return estimate
    return TokenEstimate(
        p50_tokens=estimate.p50_tokens,
        p90_tokens=math.ceil(estimate.p90_tokens * LOW_CONFIDENCE_UPPER_WIDENING),
    )


def estimate_time(estimate: TokenEstimate) -> TimeRange:
    """Convert a token estimate to a wall-clock range via the throughput band.

    The fast bound uses the typical estimate at high throughput; the slow
    bound uses the conservative estimate at low throughput.
    """
    return TimeRange(
        low_minutes=estimate.p50_tokens / THROUGHPUT_TOKENS_PER_MINUTE_HIGH,
        high_minutes=estimate.p90_tokens / THROUGHPUT_TOKENS_PER_MINUTE_LOW,
    )


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile over already sorted values."""
    if not sorted_values:
        msg = "cannot take a percentile of no values"
        raise ValueError(msg)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def update_estimate(records: Sequence[CalibrationRecord]) -> LiveEstimate | None:
    """Recompute the per-message token estimate from completed batches.

    Robust to unusual batches: per-record per-message totals more than
    :data:`MAD_CLAMP_MULTIPLIER` median absolute deviations from the median
    are discarded before the p50/p90 are taken (specification, 10.2). Records
    with zero utterances carry no per-message signal and are ignored. Returns
    ``None`` when no usable record exists.

    Callers must pass records from one compatibility partition only
    (:meth:`CalibrationRecord.partition_key`); mixing partitions is a
    programming error, not something this function corrects.
    """
    values = [
        record.total_tokens / record.utterances for record in records if record.utterances > 0
    ]
    if not values:
        return None
    center = median(values)
    mad = median(abs(value - center) for value in values)
    kept = sorted(value for value in values if abs(value - center) <= MAD_CLAMP_MULTIPLIER * mad)
    discarded = len(values) - len(kept)
    p50 = median(kept)
    p90 = max(p50, _percentile(kept, 0.9))
    return LiveEstimate(
        p50_tokens_per_message=p50,
        p90_tokens_per_message=p90,
        records_used=len(kept),
        records_discarded=discarded,
    )


def format_word_count(words: int | None) -> str:
    """Format a word count with thousands separators; blank when unknown."""
    if words is None:
        return ""
    return f"{words:,}"


def _format_hours(minutes: float) -> str:
    hours = round(minutes / 60, 1)
    if hours == int(hours):
        return str(int(hours))
    return f"{hours:.1f}"


def format_time_range(time: TimeRange | None) -> str:
    """Format a time range like ``8–12 min`` or ``1.5–2.2 h``; blank when unknown.

    Ranges whose slow bound stays under 90 minutes render in minutes;
    anything longer renders in hours, matching the specification 2.4 example.
    """
    if time is None:
        return ""
    if time.high_minutes < 90:
        return f"{round(time.low_minutes)}–{round(time.high_minutes)} min"
    return f"{_format_hours(time.low_minutes)}–{_format_hours(time.high_minutes)} h"


def render_preset_table(rows: Sequence[PresetEstimate]) -> str:
    """Render the period-preset comparison as aligned plain text.

    Columns: Period (left), Words (right), Time (left), Expected use (left).
    The layout mirrors the specification 2.4 example table.
    """
    header = ("Period", "Words", "Time", "Expected use")
    body = [
        (
            row.period,
            format_word_count(row.words),
            format_time_range(row.time),
            row.expected_use,
        )
        for row in rows
    ]
    widths = [
        max(len(header[column]), *(len(line[column]) for line in body), 0)
        if body
        else len(header[column])
        for column in range(4)
    ]
    lines: list[str] = []
    for cells in [header, *body]:
        rendered = "  ".join(
            (
                cells[0].ljust(widths[0]),
                cells[1].rjust(widths[1]),
                cells[2].ljust(widths[2]),
                cells[3],
            )
        )
        lines.append(rendered.rstrip())
    return "\n".join(lines)
