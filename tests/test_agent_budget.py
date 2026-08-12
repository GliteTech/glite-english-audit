"""The planner that stops a run from spending more agents than it is allowed.

The case these tests are written against is a real one. A run collected 1,295
messages from 395 session files across Claude Code, Codex and Cursor -- 52,669
words -- and stopped after 199 judgments because Claude Code allows 200 agents
per session and the step wanted 395 of them, with two more steps behind it.
"""

import pytest

from glite_english_audit.pipeline.agent_budget import (
    AGENT_STEPS_REMAINING,
    CLAUDE_CODE_CAP_ENV,
    DEFAULT_AGENT_CAP,
    MAX_BATCH_SESSIONS,
    MAX_BATCH_WORDS,
    AgentBudget,
    WorkItem,
    detect_agent_cap,
    plan_batches,
)

AGENT_STEPS = 3
"""Steps c, d and e. The run that ran out needed all three."""


def _budget(cap: int = DEFAULT_AGENT_CAP, *, steps: int = AGENT_STEPS) -> AgentBudget:
    return AgentBudget(cap=cap, cap_source="test", reserve=0.35, remaining_agent_steps=steps)


def _items(count: int, *, words: int = 133) -> list[WorkItem]:
    return [WorkItem(name=f"session-{n:04d}.jsonl", words=words, items=4) for n in range(count)]


def test_the_run_that_ran_out_now_fits() -> None:
    """395 sessions, 52,669 words, three steps, a cap of 200."""
    plan = plan_batches(_items(395, words=133), budget=_budget())

    assert plan.batched, "395 sessions cannot each have an agent under a cap of 200"
    assert plan.fits, "the whole point is that it now fits in one host session"
    assert plan.agents_required_all_steps <= DEFAULT_AGENT_CAP
    # Every session still judged, none split, none merged into another's file.
    assert sum(len(batch.names) for batch in plan.batches) == 395
    assert len({name for batch in plan.batches for name in batch.names}) == 395


def test_a_small_run_is_untouched() -> None:
    """One session per agent whenever it fits, because that judges best.

    Batching is a fallback. A run that can afford one context per session must
    not be given a worse one to save tokens -- the quality risk is only worth
    taking against a run that otherwise cannot finish.
    """
    plan = plan_batches(_items(40), budget=_budget())

    assert not plan.batched
    assert len(plan.batches) == 40
    assert all(len(batch.names) == 1 for batch in plan.batches)


def test_the_boundary_is_the_allowance_not_a_constant() -> None:
    """Packing starts exactly when one-per-session stops fitting."""
    allowance = _budget().per_step
    assert not plan_batches(_items(allowance), budget=_budget()).batched
    assert plan_batches(_items(allowance + 1), budget=_budget()).batched


def test_overshooting_a_little_batches_a_little() -> None:
    """The target is derived from the allowance, not a fixed size.

    A run at twice the allowance should end up with roughly two sessions per
    agent, not the maximum the ceilings permit.
    """
    allowance = _budget().per_step
    plan = plan_batches(_items(allowance * 2), budget=_budget())

    largest = max(len(batch.names) for batch in plan.batches)
    assert largest <= 4, f"packed {largest} sessions into one agent for a 2x overshoot"


def test_no_batch_exceeds_the_output_ceiling() -> None:
    """In step c the agent answers with the text it kept, so output ~ input.

    A batch that fits the context can still be truncated on the way out, and a
    truncated answer looks exactly like a judgment that dropped sentences.
    """
    plan = plan_batches(_items(4_000, words=500), budget=_budget())

    for batch in plan.batches:
        assert batch.words <= MAX_BATCH_WORDS or len(batch.names) == 1


def test_no_batch_exceeds_the_session_ceiling() -> None:
    """However tiny the sessions, attention runs out before the context does."""
    plan = plan_batches(_items(10_000, words=1), budget=_budget())

    assert max(len(batch.names) for batch in plan.batches) <= MAX_BATCH_SESSIONS


def test_a_session_larger_than_the_target_is_never_split() -> None:
    """Splitting a session is the thing this module exists not to do."""
    items = [
        WorkItem(name="session-9999.jsonl", words=50_000, items=900),
        *_items(300, words=100),
    ]
    plan = plan_batches(items, budget=_budget())

    holding = [b for b in plan.batches if "session-9999.jsonl" in b.names]
    assert len(holding) == 1, "the big session appears in exactly one batch"
    assert holding[0].names == ["session-9999.jsonl"], "and shares it with nobody"


def test_session_order_is_preserved() -> None:
    """Numbering is the only handle anyone has on a quarantined file."""
    plan = plan_batches(_items(500), budget=_budget())

    flattened = [name for batch in plan.batches for name in batch.names]
    assert flattened == sorted(flattened)


def test_the_three_steps_together_stay_inside_the_budget() -> None:
    """The bug this replaced: shares that each look fine and overrun together.

    Sizing a step's share from the steps *remaining* gave 43, 65 and 130 against
    a spendable budget of 130 -- 242 dispatches for a cap of 200, with every
    step believing itself within budget. Equal shares cannot do that.
    """
    dispatched = 0
    for step in ("c", "d", "e"):
        budget = _budget(steps=AGENT_STEPS_REMAINING[step])
        dispatched += len(plan_batches(_items(395), budget=budget).batches)

    assert dispatched <= _budget().spendable, f"{dispatched} dispatches over the whole run"
    assert dispatched <= DEFAULT_AGENT_CAP


def test_every_step_gets_the_same_share() -> None:
    """No step is entitled to more of the session than its siblings."""
    shares = {_budget(steps=AGENT_STEPS_REMAINING[s]).per_step for s in ("c", "d", "e")}
    assert len(shares) == 1


def test_the_reserve_is_not_spent_on_planned_work() -> None:
    """Repairs and retries draw on the same allowance, after planning."""
    budget = _budget()
    assert budget.spendable < budget.cap
    assert budget.per_step * AGENT_STEPS <= budget.spendable


def test_an_impossible_run_is_reported_rather_than_started_blindly() -> None:
    """When even maximal packing overruns the cap, say so before the run.

    This is the state the real run was in. It had every input needed to compute
    the number and never did, so the user learned it 199 agents later.
    """
    plan = plan_batches(_items(20_000, words=MAX_BATCH_WORDS), budget=_budget())

    assert not plan.fits
    assert plan.host_sessions_required > 1
    assert plan.agents_required_all_steps > DEFAULT_AGENT_CAP


def test_no_work_is_not_an_error() -> None:
    plan = plan_batches([], budget=_budget())

    assert plan.batches == []
    assert plan.fits
    assert plan.host_sessions_required == 1


def test_every_session_appears_exactly_once() -> None:
    """A dropped session is a silently unjudged one; a repeated one is worse."""
    for count in (1, 7, 66, 67, 395, 1_000):
        plan = plan_batches(_items(count), budget=_budget())
        seen = [name for batch in plan.batches for name in batch.names]
        assert len(seen) == count
        assert len(set(seen)) == count


def test_batch_totals_match_their_members() -> None:
    items = _items(500, words=17)
    plan = plan_batches(items, budget=_budget())

    assert sum(batch.words for batch in plan.batches) == sum(i.words for i in items)
    assert sum(batch.items for batch in plan.batches) == sum(i.items for i in items)
    assert plan.words == sum(i.words for i in items)


class TestCapDetection:
    def test_the_default_when_nothing_is_set(self) -> None:
        cap, source = detect_agent_cap(environ={})
        assert cap == DEFAULT_AGENT_CAP
        assert source == "default"

    def test_the_documented_override_is_honoured(self) -> None:
        cap, source = detect_agent_cap(environ={CLAUDE_CODE_CAP_ENV: "1200"})
        assert cap == 1200
        assert source == CLAUDE_CODE_CAP_ENV

    @pytest.mark.parametrize("value", ["", "  ", "lots", "0", "-5", "12.5"])
    def test_an_unusable_value_falls_back_and_says_so(self, value: str) -> None:
        """Silently meaning 200 is how someone raises a limit and cannot tell why."""
        cap, source = detect_agent_cap(environ={CLAUDE_CODE_CAP_ENV: value})
        assert cap == DEFAULT_AGENT_CAP
        if value.strip():
            assert CLAUDE_CODE_CAP_ENV in source, source

    def test_the_advice_the_stopped_run_gave_is_too_tight(self) -> None:
        """It told the user to set 1200. That is 1,185 needed of 1,200 available.

        98.75% utilisation, fifteen agents spare for every repair and retry in a
        run whose whole failure mode is running out. The planner keeps packing
        at that cap rather than taking the number at face value, which is the
        behaviour that makes the reserve worth having.
        """
        raised, _ = detect_agent_cap(environ={CLAUDE_CODE_CAP_ENV: "1200"})
        budget = AgentBudget(
            cap=raised, cap_source=CLAUDE_CODE_CAP_ENV, reserve=0.35, remaining_agent_steps=3
        )
        plan = plan_batches(_items(395), budget=budget)

        assert plan.fits
        assert plan.batched, "1200 does not leave room to judge 395 sessions one at a time"

    def test_a_genuinely_ample_cap_stops_the_packing(self) -> None:
        """One session per agent returns as soon as the reserve is satisfied."""
        budget = AgentBudget(
            cap=2_000, cap_source=CLAUDE_CODE_CAP_ENV, reserve=0.35, remaining_agent_steps=3
        )
        plan = plan_batches(_items(395), budget=budget)

        assert not plan.batched
        assert len(plan.batches) == 395
