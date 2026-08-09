"""Exact-shape rendering of conversation progress updates and throttle timing."""

from datetime import UTC, datetime, timedelta

import pytest

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.progress.progress import (
    STEP_TITLES,
    STEP_TOTAL,
    EstimateRange,
    ProgressState,
    ProgressThrottle,
    SourceProgress,
    _tokens,
    render_progress,
)


def test_every_step_has_exactly_one_title() -> None:
    """The user-visible step count is the pipeline's, not a number typed twice.

    ``STEP_TOTAL`` was 8 under the nine-step layout and appeared as a literal
    in the orchestration skill, so the two could disagree without anything
    failing. Deriving it from the titles makes adding a step to the enum and
    forgetting to name it a test failure instead.
    """
    assert set(STEP_TITLES) == set(StepId)
    assert len(StepId) == STEP_TOTAL
    assert len(set(STEP_TITLES.values())) == STEP_TOTAL


def test_step_titles_say_what_happens_to_the_user_not_to_the_data() -> None:
    # A person watching a run should not need the repository open to know what
    # it is doing. Internal vocabulary in a progress line is the same defect as
    # an internal word in a consent question.
    internal = ("utterance", "corpus", "artifact", "adapter", "authorship", "jsonl")
    for title in STEP_TITLES.values():
        assert not any(word in title.lower() for word in internal), title


def _state(**overrides: object) -> ProgressState:
    base: dict[str, object] = {
        "run_id": "run-test",
        "overall_percent": 23,
        "step_number": 3,
        "step_title": "Keeping only what you wrote",
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
        "Step 3 of 5: Keeping only what you wrote\n"
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
        step_number=1,
        step_title="Collecting your messages",
        per_source=[SourceProgress(label="Claude Code", done=238, total=506)],
        collected_messages=1482,
        collected_words=31620,
        est_remaining_tokens=EstimateRange(low=74000, high=112000),
        est_remaining_minutes=EstimateRange(low=12, high=19),
    )
    rendered = render_progress(state)
    assert "English audit — 8% complete" in rendered
    assert "Step 1 of 5: Collecting your messages" in rendered
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


def test_collected_totals_take_plural_nouns_above_one() -> None:
    rendered = render_progress(_state(collected_messages=2, collected_words=2))
    assert "2 eligible messages" in rendered
    assert "2 English words" in rendered


def test_collected_totals_take_singular_nouns_at_one() -> None:
    """A count of one decides the noun after it: one message, not "1 messages"."""
    rendered = render_progress(_state(collected_messages=1, collected_words=1))
    assert "1 eligible message\n" in rendered
    assert "1 English word\n" in rendered
    assert "messages" not in rendered
    assert "English words" not in rendered


def test_zero_collected_totals_stay_plural() -> None:
    rendered = render_progress(_state(collected_messages=0, collected_words=0))
    assert "0 eligible messages" in rendered
    assert "0 English words" in rendered


def test_a_source_with_one_work_unit_reads_in_the_singular() -> None:
    """A total of one takes a singular noun: 0 of 1 session, not 0 of 1 sessions."""
    rendered = render_progress(_state(per_source=[SourceProgress(label="Codex", done=0, total=1)]))
    assert "Codex: 0 of 1 session processed — 0%" in rendered


def test_a_source_measured_in_messages_is_singular_at_one_too() -> None:
    rendered = render_progress(
        _state(
            work_unit="messages",
            per_source=[SourceProgress(label="Codex", done=1, total=1)],
        )
    )
    assert "Codex: 1 of 1 message processed — 100%" in rendered


def test_an_unknown_work_unit_is_never_bent_into_a_nonword() -> None:
    """An invented singular would be worse English than a wrong plural."""
    rendered = render_progress(
        _state(work_unit="entries", per_source=[SourceProgress(label="Codex", done=0, total=1)])
    )
    assert "Codex: 0 of 1 entries processed — 0%" in rendered


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
    assert throttle.should_emit(_START + timedelta(seconds=1), step_changed=True)


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


def test_millions_of_tokens_read_as_millions() -> None:
    """A real run reaches tens of millions, and 14000K is a misreading waiting
    to happen: three orders of magnitude sit between what it says and what a
    reader skims."""
    assert _tokens(14_000_000) == "14M"
    assert _tokens(14_200_000) == "14.2M"
    assert _tokens(999_999) == "1000K"
    assert _tokens(1_500_000_000) == "1.5B"
    assert _tokens(950) == "950"


def test_the_work_unit_follows_the_step() -> None:
    """Early steps walk sessions; from step 3 on the unit is messages. Calling
    messages sessions understates a user's history by more than an order of
    magnitude."""
    state = _state()
    assert "sessions processed" in render_progress(state)
    state.work_unit = "messages"
    assert "messages processed" in render_progress(state)
    assert "sessions processed" not in render_progress(state)


def test_progress_models_have_no_field_that_could_carry_source_text() -> None:
    """Privacy by construction (specification, 9.1).

    ``ProgressState`` claims no rendered update can leak content because the
    model has no field able to hold any. That claim is only true while the
    field set is what it is, so the field set is the test. ``waiting_note`` and
    ``step_title`` are free text the orchestration writes about itself, never
    about a message; every other field is a number, an ID, or an opaque label.
    """
    assert set(ProgressState.model_fields) == {
        "run_id",
        "overall_percent",
        "step_number",
        "step_total",
        "step_title",
        "per_source",
        "collected_messages",
        "collected_words",
        "est_remaining_tokens",
        "est_remaining_minutes",
        "work_unit",
        "waiting_note",
    }
    assert set(SourceProgress.model_fields) == {"label", "done", "total"}
    assert set(EstimateRange.model_fields) == {"low", "high"}
