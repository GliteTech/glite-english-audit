"""How many agents a step may spend, and which sessions each one judges.

A run reads the learner's history in five steps, three of which are decided by
agents: keeping only what the learner wrote, finding mistakes, and checking that
no mistake carries something private. Until now each of those spent exactly one
agent per session file, which made the cost of an audit a function of how
*fragmented* someone's history is rather than how much they wrote.

That is not a hypothetical. A real run stopped mid-step with this arithmetic:

    395 session files x 3 agent steps = 1,185 agents
    Claude Code allows                    200 per session

It had judged 199 sessions -- 52,669 words across 395 files, so about 133 words
each, and every one of those agents read a skill several thousand words long to
judge them. The instructions outweighed the learner's English by roughly a
hundred to one, and the run still could not finish.

**What this module changes, and what it deliberately does not.** Only the number
of agents dispatched. The artifact is untouched: one ``session-NNNN.jsonl`` per
session in every step, verified per session, quarantined per session. That
separation is load-bearing rather than tidy. The commit that made step c
one-file-at-a-time gave the reason -- "a file that fails is quarantined whole,
the file is the unit of work, so there is no partial acceptance" -- and that
stays true when one agent answers for twelve files, because each answer is
written, verified and accepted or refused on its own. Merging sessions *on disk*
would have surrendered it, along with resume granularity and the frozen artifact
contract that lets the existing suite act as a regression proof.

The batching this replaces was a different thing: ``batches.py`` chunked a
*pooled* corpus with session boundaries dissolved, and was deleted with the pool.
Here a session is never split, never merged, and never shares a file with
another.

**Batching is a fallback, not the default.** When the work already fits, every
session gets its own agent exactly as before, because one session per context is
the best conditions a judgment can have. Packing begins only when the
alternative is a run that cannot finish, and the plan says which of the two it
chose. A quality risk taken to avoid an impossibility is a different decision
from one taken to save tokens, and this module only ever takes the first.
"""

import math
import os
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AGENT_STEPS_REMAINING",
    "AgentBudget",
    "BatchPlan",
    "PlannedBatch",
    "WorkItem",
    "detect_agent_cap",
    "plan_batches",
    "plan_step",
]

CLAUDE_CODE_CAP_ENV = "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"
"""The one documented way to raise the ceiling, verified in the shipped CLI."""

DEFAULT_AGENT_CAP = 200
"""Agents one host session allows before it refuses to start another.

Claude Code's default, confirmed by a run that stopped at 199. Codex publishes
no equivalent number; assuming it is at least as generous would turn a known
limit into a guess, so the same figure is used until a Codex run measures one.
"""

AGENT_RESERVE = 0.35
"""Share of the cap this planner refuses to spend on the planned work.

Three things draw on the same allowance after planning, and all of them are
unwelcome surprises at the end of a long run rather than the start:

* **Repairs.** A session whose judgment fails verification is re-asked, one
  agent per quarantined file. That is the mechanism which makes a bad answer
  survivable, and it needs agents left to work with.
* **Retries.** An agent that dies on a transient error is dispatched again.
* **The orchestrator's own work.** The run skill is free to spend agents on
  anything else it needs; this module does not own the whole session.

A third is a large reserve. It is deliberately large because exhausting the cap
does not degrade the run, it stops it -- and stops it at the point where the
most work has already been done and the least is recoverable.
"""

MAX_BATCH_WORDS = 8_000
"""Most of the learner's words one agent is asked to judge at once.

The binding constraint is output, not input. In step c the agent answers with
the text it kept, so its reply is roughly the size of what it read; a batch that
fits comfortably in a context window can still be truncated on the way out, and
a truncated answer is indistinguishable from a judgment that dropped sentences.
At roughly 1.4 tokens per word this ceiling is about 11k output tokens, several
times inside the smallest limit any of these models applies, and the skill's own
instructions are counted separately on top.
"""

MAX_BATCH_SESSIONS = 25
"""Most sessions one agent judges, however small they are.

A words-only ceiling would put two hundred tiny sessions in one context, where
the risk stops being length and becomes attention: the two hundredth is judged
with far less care than the first, and one careless answer now costs two hundred
files instead of one. This bound is about the blast radius of a single bad
answer as much as about accuracy.
"""

MIN_BATCH_WORDS = 400
"""Smallest batch worth forming.

Below this the packing is noise: it neither saves a meaningful number of agents
nor fills a context, and it gives up the one-session-per-context quality that is
the whole reason batching is a fallback.
"""


class WorkItem(BaseModel):
    """One session waiting for a judgment, named and measured.

    ``words`` is the learner's English in that session, which is what makes one
    assignment more work than another. Nothing here carries text, a path, or a
    session identity -- the planner decides how many agents to spend, and needs
    only sizes to do it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    words: int = Field(ge=0)
    items: int = Field(ge=0, default=0)


class PlannedBatch(BaseModel):
    """The sessions one agent is asked to judge, in one dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    names: list[str] = Field(min_length=1)
    words: int = Field(ge=0)
    items: int = Field(ge=0)


class AgentBudget(BaseModel):
    """What one host session may spend, and what is left after the reserve."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cap: int = Field(gt=0)
    cap_source: str
    """Where ``cap`` came from, so a surprising plan can be traced to a setting."""
    reserve: float = Field(ge=0.0, lt=1.0)
    remaining_agent_steps: int = Field(ge=1)
    """Agent-driven steps still to run, including this one.

    Used to report how many agents the rest of the run needs. It deliberately
    does *not* size the per-step share -- see ``per_step``.
    """

    @property
    def spendable(self) -> int:
        """Agents this planner will commit to the whole remaining run."""
        return max(1, math.floor(self.cap * (1.0 - self.reserve)))

    @property
    def per_step(self) -> int:
        """Agents this step may dispatch: an equal share of the whole session.

        Divided by every agent step, not by the ones remaining. Dividing by the
        remainder looks more generous and quietly overruns, because each step
        recomputes ``spendable`` from the full cap without knowing what the
        steps before it already spent. Measured on the 395-session run: shares
        of 43, 65 and 130 for a budget of 130, dispatching 242 agents against a
        cap of 200 -- the exact overrun the reserve exists to prevent, arrived
        at by three steps each believing itself within budget.

        Equal shares cannot do that: three of them sum to at most ``spendable``
        by construction. A run resumed straight into step e is then more
        conservative than it needed to be, which is the direction to be wrong
        in -- it batches a little harder and still finishes.
        """
        return max(1, self.spendable // TOTAL_AGENT_STEPS)


class BatchPlan(BaseModel):
    """Which agent judges what, and whether the run fits in one session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batches: list[PlannedBatch] = Field(default_factory=list)
    batched: bool
    """False when every session got its own agent, as it always did before."""
    fits: bool
    """False when even maximal packing needs more agents than the cap allows.

    The run is still worth starting -- work already judged is kept and a resumed
    run re-asks only for what is missing -- but the user has to be told before
    it begins rather than discovering it partway through.
    """
    sessions: int = Field(ge=0)
    words: int = Field(ge=0)
    per_step_allowance: int = Field(ge=0)
    agents_required_all_steps: int = Field(ge=0)
    """Agents this plan needs for every remaining agent step, not just this one.

    The number the preflight should say out loud. The run that ran out had all
    the inputs to compute it and never did.
    """
    host_sessions_required: int = Field(ge=1)
    """How many host sessions the remaining work takes at the current cap."""


def detect_agent_cap(*, environ: Mapping[str, str] | None = None) -> tuple[int, str]:
    """The subagent ceiling in force, and where it came from.

    Reads the documented override and otherwise reports the default. A value
    that cannot be read as a positive integer is ignored rather than guessed
    at, because a malformed setting silently meaning "200" is how someone
    raises a limit, sees no change, and cannot tell why.
    """
    source = environ if environ is not None else os.environ
    raw = source.get(CLAUDE_CODE_CAP_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_AGENT_CAP, f"{CLAUDE_CODE_CAP_ENV} is not a number; using the default"
        if value > 0:
            return value, CLAUDE_CODE_CAP_ENV
        return DEFAULT_AGENT_CAP, f"{CLAUDE_CODE_CAP_ENV} is not positive; using the default"
    return DEFAULT_AGENT_CAP, "default"


TOTAL_AGENT_STEPS = 3
"""Steps c, d and e. Every one of them draws on the same host-session cap."""

AGENT_STEPS_REMAINING: dict[str, int] = {"c": 3, "d": 2, "e": 1}
"""Agent-driven steps still to run once the named step begins, including it.

Step c must not spend the host session on itself: d and e draw on the same cap
and cannot ask for more once it is gone.
"""


def plan_step(
    items: Sequence[WorkItem],
    *,
    step: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[BatchPlan, AgentBudget]:
    """Plan one step's dispatch against the ceiling actually in force.

    ``items`` should be the work still outstanding, not every session in the
    step. A resumed run has answers on disk already, and planning as though it
    did not would pack a run that no longer needs packing.
    """
    cap, source = detect_agent_cap(environ=environ)
    budget = AgentBudget(
        cap=cap,
        cap_source=source,
        reserve=AGENT_RESERVE,
        remaining_agent_steps=AGENT_STEPS_REMAINING[step],
    )
    return plan_batches(items, budget=budget), budget


def _pack(items: Sequence[WorkItem], *, target_words: int) -> list[list[WorkItem]]:
    """Fill batches in order, closing one when the next session would overflow.

    Order is preserved rather than sorted by size. Sorting would pack more
    tightly, but session numbering is the only handle anyone has on a run -- a
    quarantined file, a resumed step, a diagnostic -- and a plan that reorders
    them makes every one of those harder to follow for a few percent of density.

    A session larger than ``target_words`` becomes its own batch instead of
    being split, because splitting a session is what this module exists not to
    do.
    """
    batches: list[list[WorkItem]] = []
    current: list[WorkItem] = []
    current_words = 0
    for item in items:
        would_exceed_words = current and current_words + item.words > target_words
        would_exceed_count = len(current) >= MAX_BATCH_SESSIONS
        if would_exceed_words or would_exceed_count:
            batches.append(current)
            current, current_words = [], 0
        current.append(item)
        current_words += item.words
    if current:
        batches.append(current)
    return batches


def plan_batches(
    items: Sequence[WorkItem],
    *,
    budget: AgentBudget,
) -> BatchPlan:
    """Decide how many agents this step spends, and which sessions each judges.

    One session per agent whenever that fits the budget, because that is the
    best conditions a judgment can have and there is no reason to spend it.
    Packing starts only when one-per-session would need more agents than the
    step is allowed, and then packs no harder than it must: the target is
    derived from the allowance, so a run that overshoots by a little is batched
    a little.
    """
    work = list(items)
    total_words = sum(item.words for item in work)
    allowance = budget.per_step

    if not work:
        return BatchPlan(
            batches=[],
            batched=False,
            fits=True,
            sessions=0,
            words=0,
            per_step_allowance=allowance,
            agents_required_all_steps=0,
            host_sessions_required=1,
        )

    if len(work) <= allowance:
        grouped = [[item] for item in work]
        batched = False
    else:
        # Derived, not fixed. Enough sessions per agent to come in under the
        # allowance and no more, so quality is given up in proportion to the
        # problem rather than wholesale.
        #
        # Then grown until it actually fits. A first-fit packer does not land on
        # the arithmetic target: with 395 sessions of 133 words and an allowance
        # of 43, a target of 1,222 words takes nine sessions per batch and
        # produces 44 -- one over, every step, which is how three steps that each
        # believe themselves inside the budget dispatch more than the cap allows.
        # Growing beats shrinking the reserve, and the ceilings still bind: when
        # MAX_BATCH_WORDS is reached the plan stops packing and reports that the
        # work does not fit, which is a true answer rather than a silent overrun.
        target = max(MIN_BATCH_WORDS, math.ceil(total_words / allowance))
        target = min(target, MAX_BATCH_WORDS)
        grouped = _pack(work, target_words=target)
        while len(grouped) > allowance and target < MAX_BATCH_WORDS:
            target = min(MAX_BATCH_WORDS, max(target + MIN_BATCH_WORDS, math.ceil(target * 1.2)))
            grouped = _pack(work, target_words=target)
        batched = True

    batches = [
        PlannedBatch(
            index=index,
            names=[item.name for item in group],
            words=sum(item.words for item in group),
            items=sum(item.items for item in group),
        )
        for index, group in enumerate(grouped)
    ]
    required_all_steps = len(batches) * budget.remaining_agent_steps
    return BatchPlan(
        batches=batches,
        batched=batched,
        # Against the real cap, not the reserved allowance: the reserve exists
        # to absorb repairs, and calling a run impossible because its repairs
        # might not fit would refuse work that will almost certainly complete.
        fits=required_all_steps <= budget.cap,
        sessions=len(work),
        words=total_words,
        per_step_allowance=allowance,
        agents_required_all_steps=required_all_steps,
        host_sessions_required=max(1, math.ceil(required_all_steps / budget.cap)),
    )
