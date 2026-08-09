"""Progress reporting for the agent conversation (specification, 9.1-9.2).

The progress surface is the agent conversation, not a browser dashboard. The
:class:`ProgressState` model carries only opaque source labels and numbers, so
no raw message content can enter a rendered update by construction.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.english import plural, singularize

STEP_TITLES: dict[StepId, str] = {
    StepId.A_COLLECTED: "Collecting your messages",
    StepId.B_DEDUPLICATED: "Removing what you said twice",
    StepId.C_AUTHORED: "Keeping only what you wrote",
    StepId.D_MISTAKES: "Finding English mistakes",
    StepId.E_VERIFIED: "Checking nothing private got through",
}
"""The step titles the user sees, written once so they cannot drift.

A skill that retypes these produces a different name for the same step in the
same run, and the person reading the update has no way to tell whether they are
watching one step or two.
"""

STEP_TOTAL = len(STEP_TITLES)
"""User-visible steps in one audit: the five the pipeline actually has.

Discovery happens during setup, before the run exists, and the review is not a
step — it reads step e and writes into ``submission/``. Counting either of them
here would report a run as less finished than it is.
"""

MIN_EMIT_INTERVAL_SECONDS = 10.0
MAX_EMIT_INTERVAL_SECONDS = 60.0


class SourceProgress(BaseModel):
    """Per-source progress: an opaque display label plus work-unit counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    done: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _done_within_total(self) -> "SourceProgress":
        if self.done > self.total:
            msg = f"done ({self.done}) exceeds total ({self.total})"
            raise ValueError(msg)
        return self


class EstimateRange(BaseModel):
    """A low-high estimate range; reported because uncertainty is meaningful."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low: int = Field(ge=0)
    high: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "EstimateRange":
        if self.low > self.high:
            msg = f"low ({self.low}) exceeds high ({self.high})"
            raise ValueError(msg)
        return self


class ProgressState(BaseModel):
    """Everything one conversation progress update is rendered from.

    Only labels and numbers: the model has no field that could carry source
    text, so a rendered update cannot leak content (specification, 9.1).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    overall_percent: int = Field(ge=0, le=100)
    step_number: int = Field(ge=1)
    step_total: int = Field(default=STEP_TOTAL, ge=1)
    step_title: str
    per_source: list[SourceProgress] = Field(default_factory=list)
    collected_messages: int = Field(ge=0)
    collected_words: int = Field(ge=0)
    est_remaining_tokens: EstimateRange
    est_remaining_minutes: EstimateRange
    work_unit: str = "sessions"
    """Plural noun for what the per-source counts count. Step a walks sessions;
    from step c on the unit is messages, and calling those sessions tells the
    user their history is twenty times smaller than it is."""
    waiting_note: str | None = None
    """Set when the previous update was delayed by an uninterruptible
    provider call; rendered as a trailing note (specification, 9.1)."""

    @model_validator(mode="after")
    def _step_within_total(self) -> "ProgressState":
        if self.step_number > self.step_total:
            msg = f"step_number ({self.step_number}) exceeds step_total ({self.step_total})"
            raise ValueError(msg)
        return self


def _percent(done: int, total: int) -> int:
    """Floor percentage; never reports 100% before the work is done."""
    if total <= 0:
        return 0
    return min(100, done * 100 // total)


def _count(value: int) -> str:
    return f"{value:,}"


def _tokens(value: int) -> str:
    """Scale a token count to a unit a reader can hold in their head.

    Real runs reach tens of millions, and the thousands-only form rendered
    that as ``14000K``, which is both unreadable and easy to misread by three
    orders of magnitude. One decimal place survives at the M and B tiers
    because the difference between 14.2M and 14.9M is the difference between
    two estimates.
    """
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}".removesuffix(".0") + "B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".removesuffix(".0") + "M"
    if value >= 1000:
        return f"{round(value / 1000)}K"
    return str(value)


def render_progress(state: ProgressState) -> str:
    """Render one conversation progress update in the specification 9.1 shape."""
    step_done = sum(source.done for source in state.per_source)
    step_total = sum(source.total for source in state.per_source)
    step_percent = _percent(step_done, step_total)

    lines = [
        f"English audit — {state.overall_percent}% complete",
        "",
        f"Step {state.step_number} of {state.step_total}: {state.step_title}",
    ]
    for source in state.per_source:
        lines.append(
            f"{source.label}: {_count(source.done)} of {_count(source.total)} "
            f"{singularize(source.total, state.work_unit)} processed — "
            f"{_percent(source.done, source.total)}%"
        )
    tokens = state.est_remaining_tokens
    minutes = state.est_remaining_minutes
    lines += [
        f"This step: {step_percent}% · Overall: {state.overall_percent}%",
        "",
        "Collected so far:",
        f"{_count(state.collected_messages)} eligible "
        f"{plural(state.collected_messages, 'message')}",
        f"{_count(state.collected_words)} English {plural(state.collected_words, 'word')}",
        "",
        f"Estimated remaining: {_tokens(tokens.low)}–{_tokens(tokens.high)} tokens",
        f"Estimated time: {minutes.low}–{minutes.high} minutes",
    ]
    if state.waiting_note is not None:
        lines += ["", state.waiting_note]
    return "\n".join(lines)


class ProgressThrottle:
    """Rate limit for conversation updates (specification, 9.1).

    At most one update per ``min_interval_seconds`` unless a stage changes or
    a material warning occurs; an update becomes overdue after
    ``max_interval_seconds``. The clock is injected through ``now`` so tests
    never sleep.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = MIN_EMIT_INTERVAL_SECONDS,
        max_interval_seconds: float = MAX_EMIT_INTERVAL_SECONDS,
    ) -> None:
        if min_interval_seconds > max_interval_seconds:
            msg = "min_interval_seconds exceeds max_interval_seconds"
            raise ValueError(msg)
        self._min_interval_seconds = min_interval_seconds
        self._max_interval_seconds = max_interval_seconds
        self._last_emit_at: datetime | None = None

    def should_emit(
        self,
        now: datetime,
        *,
        stage_changed: bool = False,
        warning: bool = False,
    ) -> bool:
        """Decide whether to emit an update now; records the emit when True."""
        emit = (
            self._last_emit_at is None
            or stage_changed
            or warning
            or (now - self._last_emit_at).total_seconds() >= self._min_interval_seconds
        )
        if emit:
            self._last_emit_at = now
        return emit

    def overdue(self, now: datetime) -> bool:
        """True when the at-least-once-per-60-seconds obligation is due."""
        if self._last_emit_at is None:
            return True
        return (now - self._last_emit_at).total_seconds() >= self._max_interval_seconds
