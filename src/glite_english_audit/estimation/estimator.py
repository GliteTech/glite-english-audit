"""Pure token and time estimators over calibration coefficients.

Every function here is deterministic over its inputs: profile coefficients
(specification, 10.1), private cross-run history records (10.3), and candidate
counts from local scripts (2.4). Estimates are ranges, never guarantees, and
low-confidence cells widen the upper bound (13.7).
"""

import math
import textwrap
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
#
# These bounds predate the 2026-08-08 real-data profile and do not fit it: the
# measured claude-code cells are dominated by cached input, which the provider
# re-reads far faster than it generates output, so dividing their totals by a
# fresh-token throughput overstates duration by roughly two orders of
# magnitude. Preset and preflight durations use :func:`estimate_unit_time`.
THROUGHPUT_TOKENS_PER_MINUTE_LOW: int = 4000
THROUGHPUT_TOKENS_PER_MINUTE_HIGH: int = 9000

# Wall-clock band per processed unit inside one worker. Duration tracks units,
# not tokens: each unit is one agentic loop with a file write, and its cost is
# turn latency rather than token volume. Measured in the 2026-08-08 real-data
# calibration (temp/findings/token-calibration-method.md): ten concurrent
# find-mistakes batches of 25 units, launched together, finished 105-190
# seconds later — 4.2-7.6 seconds per unit per worker. One sample on one
# machine and one runtime, so the upper bound is widened by half.
SECONDS_PER_UNIT_LOW: float = 4.2
SECONDS_PER_UNIT_HIGH: float = 11.4

# Downstream volume per candidate message, from the same calibration run: 250
# find-mistakes units produced the findings that 100 verify-findings units
# re-checked, and 45 of those findings reached create-safe-records. Both ratios
# move with the corpus and with the strict threshold, so they are estimates,
# not constants of the product.
VERIFY_UNITS_PER_MESSAGE: float = 0.40
SAFE_RECORD_UNITS_PER_MESSAGE: float = 0.18

# What survives the stage-3 authorship judgment, measured on the 2026-08-09
# real-data run: of 198 candidate utterances holding 8,956 words, 192
# utterances and 4,265 words were judged the learner's own. Nearly every
# utterance keeps something, but under half its words do, because the bulk of
# what a learner pastes into a coding agent was written by someone else.
#
# Every step after stage 3 reads the retained text, so estimating them from the
# candidate word count overstates them by roughly a factor of two. Both ratios
# move with the corpus — a learner who pastes less keeps more.
AUTHORED_WORD_RETENTION: float = 0.48
AUTHORED_UTTERANCE_RETENTION: float = 0.97

# Profile step identifiers for the four calibrated semantic steps.
STEP_JUDGE_AUTHORSHIP: str = "judge-authorship"
STEP_FIND_MISTAKES: str = "find-mistakes"
STEP_VERIFY_FINDINGS: str = "verify-findings"
STEP_CREATE_SAFE_RECORDS: str = "create-safe-records"

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


def estimate_unit_time(units: int, *, concurrent_batches: int = 1) -> TimeRange:
    """Wall-clock range for a semantic step that processes ``units`` items.

    Uses the measured per-unit band rather than a token throughput, because a
    unit costs turn latency and the token totals are mostly cached input.
    ``concurrent_batches`` divides the range: one worker is the conservative
    default, since nothing in the orchestration guarantees parallelism.
    """
    if units < 0:
        msg = "units must be non-negative"
        raise ValueError(msg)
    if concurrent_batches < 1:
        msg = "concurrent_batches must be at least 1"
        raise ValueError(msg)
    divisor = 60.0 * concurrent_batches
    return TimeRange(
        low_minutes=units * SECONDS_PER_UNIT_LOW / divisor,
        high_minutes=units * SECONDS_PER_UNIT_HIGH / divisor,
    )


def apply_time_confidence(time: TimeRange, confidence: EstimateConfidence) -> TimeRange:
    """Widen the slow bound of a low-confidence duration range."""
    if confidence is EstimateConfidence.HIGH:
        return time
    return TimeRange(
        low_minutes=time.low_minutes,
        high_minutes=time.high_minutes * LOW_CONFIDENCE_UPPER_WIDENING,
    )


def profile_batches(entry: TokenUsageProfileEntry) -> int:
    """Completed batches behind a committed cell, at the assumed batch size.

    The confidence rule counts batches (specification, 13.7) while the profile
    records messages, so the committed sample is converted here rather than at
    each call site.
    """
    return entry.messages_measured // ASSUMED_BATCH_SIZE


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


def _format_token_count(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        scaled = f"{tokens / 1_000_000_000:.1f}".removesuffix(".0")
        return f"{scaled}B"
    if tokens >= 1_000_000:
        scaled = f"{tokens / 1_000_000:.1f}".removesuffix(".0")
        return f"{scaled}M"
    if tokens >= 1_000:
        return f"{round(tokens / 1_000)}K"
    return str(tokens)


def format_token_range(estimate: TokenEstimate | None) -> str:
    """Format a token estimate like ``0.9M–1.6M``; blank when unknown."""
    if estimate is None:
        return ""
    return f"{_format_token_count(estimate.p50_tokens)}–{_format_token_count(estimate.p90_tokens)}"


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


def render_estimate_report(
    rows: Sequence[PresetEstimate], *, notes: Sequence[str], width: int = 88
) -> str:
    """The preset table plus the caveats that must travel with its numbers.

    The table alone reads as measurement. Interpolated word counts, an
    uncalibrated cell, and an unavailable price all have to reach the user
    with the numbers rather than in a separate paragraph a caller may drop, so
    they are rendered into the same block. Notes wrap; the table never does,
    because a wrapped column stops being a column.
    """
    table = render_preset_table(rows)
    if not notes:
        return table
    wrapped = [
        textwrap.fill(note, width=width, initial_indent="- ", subsequent_indent="  ")
        for note in notes
    ]
    return table + "\n\n" + "\n".join(wrapped)
