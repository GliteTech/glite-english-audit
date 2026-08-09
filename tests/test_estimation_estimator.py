"""Tests for the pure token/time estimators and the preset table renderer."""

from datetime import UTC, datetime

import pytest

from glite_english_audit.estimation.estimator import (
    HIGH_CONFIDENCE_MIN_RECORDS,
    EstimateConfidence,
    PresetEstimate,
    TimeRange,
    TokenEstimate,
    apply_confidence,
    confidence_for,
    estimate_run,
    estimate_step,
    estimate_time,
    format_time_range,
    format_word_count,
    profile_batches,
    render_preset_table,
    update_estimate,
)
from glite_english_audit.estimation.profile import CalibrationRecord, TokenUsageProfileEntry


def _entry(**overrides: object) -> TokenUsageProfileEntry:
    fields: dict[str, object] = {
        "step": "find-mistakes",
        "runtime": "claude-code",
        "model": "pinned-model-id",
        "effort": "medium",
        "messages_measured": 500,
        "average_words_per_message": 40.0,
        "fixed_input_tokens_per_batch": 1000,
        "input_tokens_per_message": 80.0,
        "input_tokens_per_word": 2.0,
        "cached_input_tokens_per_message": 10.0,
        "output_tokens_per_message": 10.0,
        "retry_rate": 0.0,
        "p50_total_tokens_per_message": 100.0,
        "p90_total_tokens_per_message": 150.0,
    }
    fields.update(overrides)
    return TokenUsageProfileEntry.model_validate(fields)


def _record(total_tokens: int, **overrides: object) -> CalibrationRecord:
    fields: dict[str, object] = {
        "runtime": "claude-code",
        "model": "pinned-model-id",
        "effort": "medium",
        "step": "find-mistakes",
        "skill_version": 1,
        "prompt_version": 1,
        "schema_version": 1,
        "batch_size": 25,
        "words": 1000,
        "utterances": 25,
        "fresh_input_tokens": total_tokens - 400,
        "cached_input_tokens": 250,
        "output_tokens": 150,
        "retries": 0,
        "duration_seconds": 30.0,
        "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return CalibrationRecord.model_validate(fields)


def test_estimate_stage_known_inputs() -> None:
    # 50 messages at the calibrated 40 words each: no word adjustment.
    # ceil(50 / 7) = 8 calls add 8 * 1000 fixed tokens.
    estimate = estimate_step(2000, 50, _entry())
    assert estimate == TokenEstimate(p50_tokens=13000, p90_tokens=15500)


def test_estimate_stage_word_adjustment_scales_input() -> None:
    # 100 words above the calibrated average add 100 * 2.0 input tokens.
    estimate = estimate_step(2100, 50, _entry())
    assert estimate == TokenEstimate(p50_tokens=13200, p90_tokens=15700)


def test_estimate_stage_zero_utterances_is_zero() -> None:
    assert estimate_step(0, 0, _entry()) == TokenEstimate(p50_tokens=0, p90_tokens=0)


def test_estimate_stage_never_drops_below_fixed_overhead() -> None:
    # An extreme negative word delta cannot push the estimate below the fixed
    # prompt overhead, which is paid once per call whatever the text is.
    estimate = estimate_step(0, 50, _entry(input_tokens_per_word=4.0))
    assert estimate.p50_tokens == 8000
    assert estimate.p90_tokens >= estimate.p50_tokens


def test_a_call_is_a_session_file_not_a_batch_of_25() -> None:
    """The fixed prompt is paid per call, and a call is now one session file.

    Measured on a real run: 141 messages in 20 sessions. Amortizing over 25
    would charge 6 calls for work that makes 20, understating the fixed
    overhead by more than a third — and the fixed overhead is ~100K tokens per
    call in the committed profile, not a rounding error.
    """
    entry = _entry(input_tokens_per_word=0.0)
    per_message = 141 * 100  # p50_total_tokens_per_message
    assert estimate_step(141 * 40, 141, entry).p50_tokens == per_message + 21 * 1000
    assert (
        estimate_step(141 * 40, 141, entry, messages_per_call=25).p50_tokens
        == per_message + 6 * 1000
    )


def test_the_confidence_sample_count_does_not_move_with_the_grouping() -> None:
    # How many calibration samples were taken is a fact about the measurement.
    # Dividing it by the pipeline's current grouping would turn the same
    # evidence into more samples and flip low-confidence cells to high.
    assert profile_batches(_entry(messages_measured=198)) == 198 // 25


def test_estimate_stage_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        estimate_step(-1, 10, _entry())
    with pytest.raises(ValueError, match="messages_per_call"):
        estimate_step(100, 10, _entry(), messages_per_call=0)


def test_estimate_run_sums_stage_estimates() -> None:
    total = estimate_run(
        [
            TokenEstimate(p50_tokens=7000, p90_tokens=9500),
            TokenEstimate(p50_tokens=3000, p90_tokens=4500),
        ]
    )
    assert total == TokenEstimate(p50_tokens=10000, p90_tokens=14000)


def test_confidence_boundary_at_ten_records() -> None:
    entry = _entry()
    below = confidence_for(entry, compatible_history_records=HIGH_CONFIDENCE_MIN_RECORDS - 1)
    at_boundary = confidence_for(entry, compatible_history_records=HIGH_CONFIDENCE_MIN_RECORDS)
    assert below is EstimateConfidence.LOW
    assert at_boundary is EstimateConfidence.HIGH


def test_uncalibrated_entry_is_low_confidence_without_history() -> None:
    entry = _entry(messages_measured=0)
    assert confidence_for(entry, compatible_history_records=0) is EstimateConfidence.LOW
    assert confidence_for(entry, compatible_history_records=10) is EstimateConfidence.HIGH


def test_apply_confidence_widens_upper_bound_by_half() -> None:
    estimate = TokenEstimate(p50_tokens=1000, p90_tokens=2000)
    widened = apply_confidence(estimate, EstimateConfidence.LOW)
    assert widened == TokenEstimate(p50_tokens=1000, p90_tokens=3000)
    assert apply_confidence(estimate, EstimateConfidence.HIGH) == estimate


def test_estimate_time_uses_throughput_band() -> None:
    time = estimate_time(TokenEstimate(p50_tokens=9000, p90_tokens=12000))
    assert time.low_minutes == pytest.approx(1.0)
    assert time.high_minutes == pytest.approx(3.0)


def test_update_estimate_discards_mad_outlier() -> None:
    # Per-message totals: 98, 99, 100, 101, 102, then one wild 5000 batch.
    per_message = [98, 99, 100, 101, 102, 5000]
    records = [_record(value * 25) for value in per_message]
    live = update_estimate(records)
    assert live is not None
    assert live.records_used == 5
    assert live.records_discarded == 1
    assert live.p50_tokens_per_message == pytest.approx(100.0)
    assert live.p90_tokens_per_message == pytest.approx(101.6)


def test_update_estimate_identical_records_keep_everything() -> None:
    records = [_record(2500) for _ in range(4)]
    live = update_estimate(records)
    assert live is not None
    assert live.records_used == 4
    assert live.records_discarded == 0
    assert live.p50_tokens_per_message == pytest.approx(100.0)
    assert live.p90_tokens_per_message == pytest.approx(100.0)


def test_update_estimate_without_usable_records_is_none() -> None:
    assert update_estimate([]) is None
    assert update_estimate([_record(2500, utterances=0)]) is None


def test_format_word_count() -> None:
    assert format_word_count(18400) == "18,400"
    assert format_word_count(None) == ""


def test_format_time_range_switches_units_at_ninety_minutes() -> None:
    assert format_time_range(TimeRange(low_minutes=8, high_minutes=12)) == "8–12 min"
    assert format_time_range(TimeRange(low_minutes=29, high_minutes=41)) == "29–41 min"
    assert format_time_range(TimeRange(low_minutes=90, high_minutes=132)) == "1.5–2.2 h"
    assert format_time_range(TimeRange(low_minutes=300, high_minutes=420)) == "5–7 h"
    assert format_time_range(None) == ""


def test_render_preset_table_snapshot() -> None:
    rows = [
        PresetEstimate(
            period="Last 7 days",
            words=18400,
            time=TimeRange(low_minutes=8, high_minutes=12),
            expected_use="6–9% of your remaining 5-hour limit",
        ),
        PresetEstimate(
            period="Last 30 days",
            words=72100,
            time=TimeRange(low_minutes=29, high_minutes=41),
            expected_use="24–34% of your remaining 5-hour limit",
        ),
        PresetEstimate(
            period="Last 3 months",
            words=218000,
            time=TimeRange(low_minutes=90, high_minutes=132),
            expected_use="73–105% — may require a limit reset",
        ),
        PresetEstimate(
            period="Last year",
            words=714000,
            time=TimeRange(low_minutes=300, high_minutes=420),
            expected_use="More than the currently available limit",
        ),
        PresetEstimate(
            period="Everything",
            words=981000,
            time=TimeRange(low_minutes=420, high_minutes=600),
            expected_use="More than the currently available limit",
        ),
        PresetEstimate(
            period="Custom dates",
            expected_use="Calculated after dates are entered",
        ),
    ]
    expected = (
        "Period           Words  Time       Expected use\n"
        "Last 7 days     18,400  8–12 min   6–9% of your remaining 5-hour limit\n"
        "Last 30 days    72,100  29–41 min  24–34% of your remaining 5-hour limit\n"
        "Last 3 months  218,000  1.5–2.2 h  73–105% — may require a limit reset\n"
        "Last year      714,000  5–7 h      More than the currently available limit\n"
        "Everything     981,000  7–10 h     More than the currently available limit\n"
        "Custom dates                       Calculated after dates are entered"
    )
    assert render_preset_table(rows) == expected


def test_render_preset_table_empty_rows_is_header_only() -> None:
    assert render_preset_table([]) == "Period  Words  Time  Expected use"
