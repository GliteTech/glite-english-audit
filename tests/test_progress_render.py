"""Exact-shape rendering of conversation progress updates and throttle timing."""

from datetime import UTC, datetime, timedelta

import pytest

from glite_english_audit.progress.progress import (
    EstimateRange,
    ProgressState,
    ProgressThrottle,
    SourceProgress,
    render_progress,
)


def _state(**overrides: object) -> ProgressState:
    base: dict[str, object] = {
        "run_id": "run-test",
        "overall_percent": 23,
        "step_number": 3,
        "step_title": "Filtering authored English",
        "per_source": [
            SourceProgress(label="Claude Code", done=120, total=400),
            SourceProgress(label="Codex", done=10, total=50),
        ],
        "collected_messages": 2413,
        "collected_words": 52310,
        "est_remaining_tokens": EstimateRange(low=84000, high=120000),
        "est_remaining_minutes": EstimateRange(low=14, high=22),
    }
    base.update(overrides)
    return ProgressState.model_validate(base)


def test_render_matches_spec_shape_exactly() -> None:
    expected = (
        "English audit — 23% complete\n"
        "\n"
        "Step 3 of 8: Filtering authored English\n"
        "Claude Code: 120 of 400 sessions processed — 30%\n"
        "Codex: 10 of 50 sessions processed — 20%\n"
        "This step: 28% · Overall: 23%\n"
        "\n"
        "Collected so far:\n"
        "2,413 eligible messages\n"
        "52,310 English words\n"
        "\n"
        "Estimated remaining: 84K–120K tokens\n"
        "Estimated time: 14–22 minutes"
    )
    assert render_progress(_state()) == expected


def test_render_single_source_percentages() -> None:
    state = _state(
        overall_percent=8,
        step_number=2,
        step_title="Collecting selected English",
        per_source=[SourceProgress(label="Claude Code", done=238, total=506)],
        collected_messages=1482,
        collected_words=31620,
        est_remaining_tokens=EstimateRange(low=74000, high=112000),
        est_remaining_minutes=EstimateRange(low=12, high=19),
    )
    rendered = render_progress(state)
    assert "English audit — 8% complete" in rendered
    assert "Step 2 of 8: Collecting selected English" in rendered
    assert "Claude Code: 238 of 506 sessions processed — 47%" in rendered
    assert "This step: 47% · Overall: 8%" in rendered
    assert "1,482 eligible messages" in rendered
    assert "31,620 English words" in rendered
    assert "Estimated remaining: 74K–112K tokens" in rendered
    assert "Estimated time: 12–19 minutes" in rendered


def test_render_waiting_note_is_trailing_block() -> None:
    note = "The previous update was delayed while waiting for a provider response."
    rendered = render_progress(_state(waiting_note=note))
    assert rendered.endswith(f"\n\n{note}")


def test_render_small_token_counts_stay_plain() -> None:
    state = _state(est_remaining_tokens=EstimateRange(low=800, high=950))
    assert "Estimated remaining: 800–950 tokens" in render_progress(state)


def test_render_zero_totals_render_zero_percent() -> None:
    state = _state(per_source=[SourceProgress(label="Codex", done=0, total=0)])
    assert "Codex: 0 of 0 sessions processed — 0%" in render_progress(state)
    assert "This step: 0% · Overall: 23%" in render_progress(state)


def test_state_rejects_done_beyond_total() -> None:
    with pytest.raises(ValueError, match="exceeds total"):
        SourceProgress(label="Codex", done=5, total=4)


def test_state_rejects_inverted_estimate_range() -> None:
    with pytest.raises(ValueError, match="exceeds high"):
        EstimateRange(low=10, high=5)


def test_state_rejects_step_beyond_total() -> None:
    with pytest.raises(ValueError, match="exceeds step_total"):
        _state(step_number=9)


_START = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)


def test_throttle_first_call_emits() -> None:
    throttle = ProgressThrottle()
    assert throttle.should_emit(_START)


def test_throttle_suppresses_within_ten_seconds() -> None:
    throttle = ProgressThrottle()
    assert throttle.should_emit(_START)
    assert not throttle.should_emit(_START + timedelta(seconds=9))
    assert throttle.should_emit(_START + timedelta(seconds=10))


def test_throttle_suppression_does_not_reset_the_clock() -> None:
    throttle = ProgressThrottle()
    assert throttle.should_emit(_START)
    assert not throttle.should_emit(_START + timedelta(seconds=5))
    assert throttle.should_emit(_START + timedelta(seconds=10))


def test_throttle_stage_change_bypasses_minimum() -> None:
    throttle = ProgressThrottle()
    assert throttle.should_emit(_START)
    assert throttle.should_emit(_START + timedelta(seconds=1), stage_changed=True)


def test_throttle_warning_bypasses_minimum() -> None:
    throttle = ProgressThrottle()
    assert throttle.should_emit(_START)
    assert throttle.should_emit(_START + timedelta(seconds=1), warning=True)


def test_throttle_overdue_after_sixty_seconds() -> None:
    throttle = ProgressThrottle()
    assert throttle.overdue(_START)
    assert throttle.should_emit(_START)
    assert not throttle.overdue(_START + timedelta(seconds=59))
    assert throttle.overdue(_START + timedelta(seconds=60))


def test_throttle_rejects_inverted_intervals() -> None:
    with pytest.raises(ValueError, match="exceeds max_interval_seconds"):
        ProgressThrottle(min_interval_seconds=61.0, max_interval_seconds=60.0)
