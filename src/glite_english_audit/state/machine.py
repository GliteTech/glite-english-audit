"""Explicit transition tables for run and step lifecycles.

Every transition the orchestration may perform is listed here; anything else
raises :class:`InvalidTransitionError` carrying the stable
``STATE_INVALID_TRANSITION`` diagnostic code. Tests enumerate the tables, so a
new transition is a reviewed contract change, not an accident.
"""

from glite_english_audit.artifacts.enums import RunStatus, StepId, StepStatus
from glite_english_audit.diagnostics.codes import Diagnostic

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.SELECTING, RunStatus.EXPIRED}),
    RunStatus.SELECTING: frozenset({RunStatus.AWAITING_PREFLIGHT, RunStatus.EXPIRED}),
    RunStatus.AWAITING_PREFLIGHT: frozenset({RunStatus.PROCESSING, RunStatus.EXPIRED}),
    RunStatus.PROCESSING: frozenset(
        {
            RunStatus.CHECKPOINTED,
            RunStatus.BLOCKED,
            RunStatus.REVIEW,
            RunStatus.COMPLETED_WITH_EXCLUSIONS,
        }
    ),
    RunStatus.CHECKPOINTED: frozenset(
        {RunStatus.PROCESSING, RunStatus.AWAITING_PREFLIGHT, RunStatus.EXPIRED}
    ),
    RunStatus.BLOCKED: frozenset(
        {RunStatus.PROCESSING, RunStatus.AWAITING_PREFLIGHT, RunStatus.EXPIRED}
    ),
    # A review the user abandons must still reach EXPIRED, or its step 4-7
    # artifacts keep raw source language past the 30-day retention rule
    # (specification, 3.6). Reopening a review re-enters processing.
    RunStatus.REVIEW: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_EXCLUSIONS,
            RunStatus.PROCESSING,
            RunStatus.EXPIRED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.COMPLETED_WITH_EXCLUSIONS: frozenset(),
    RunStatus.EXPIRED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.IN_PROGRESS}),
    StepStatus.IN_PROGRESS: frozenset(
        {StepStatus.PRODUCED, StepStatus.FAILED, StepStatus.QUARANTINED}
    ),
    StepStatus.PRODUCED: frozenset({StepStatus.VERIFIED_DETERMINISTIC, StepStatus.FAILED}),
    StepStatus.VERIFIED_DETERMINISTIC: frozenset(
        {StepStatus.VERIFIED_SEMANTIC, StepStatus.PROMOTED, StepStatus.FAILED}
    ),
    StepStatus.VERIFIED_SEMANTIC: frozenset({StepStatus.PROMOTED, StepStatus.FAILED}),
    # Promotion is not terminal: replacement after repair re-enters production,
    # and an upstream replacement invalidates this step (specification, 6.5).
    StepStatus.PROMOTED: frozenset({StepStatus.IN_PROGRESS, StepStatus.INVALIDATED}),
    StepStatus.QUARANTINED: frozenset({StepStatus.IN_PROGRESS}),
    StepStatus.FAILED: frozenset({StepStatus.IN_PROGRESS}),
    StepStatus.INVALIDATED: frozenset({StepStatus.IN_PROGRESS}),
}

SEMANTIC_STEPS: frozenset[StepId] = frozenset()
"""Steps that may not promote until a second, independent reader has passed.

Empty, and deliberately so. This set exists to forbid the jump from
``VERIFIED_DETERMINISTIC`` to ``PROMOTED``, forcing a ``VERIFIED_SEMANTIC``
step in between. Under the nine-step design three steps qualified, each with
its own independent verifier.

Under the five steps none does, and each for its own reason:

- **c** is model judgment checked by a deterministic span verifier. A decision
  whose spans do not appear verbatim is quarantined rather than repaired, so
  there is nothing for a second reader to adjudicate.
- **d** had an independent findings verifier and no longer does. The owner
  removed it on 2026-08-09, choosing to fix the extraction skill when quality
  slips rather than add a skill that checks it. Requiring a semantic pass here
  would demand a verifier the product does not have.
- **e** *is* the second reader. It cannot wait on itself.

The mechanism is kept rather than deleted because this is where a verifier
goes if one is added back, and an empty set that says why is clearer than a
missing concept.
"""


class InvalidTransitionError(Exception):
    """A forbidden lifecycle transition was attempted."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def can_advance_run(current: RunStatus, target: RunStatus) -> bool:
    """True when the run may move from ``current`` to ``target``."""
    return target in _RUN_TRANSITIONS[current]


def advance_run(current: RunStatus, target: RunStatus) -> RunStatus:
    """Validate and perform a run transition."""
    if not can_advance_run(current, target):
        raise InvalidTransitionError(
            Diagnostic.from_code(
                "STATE_INVALID_TRANSITION",
                f"run may not move from {current.value!r} to {target.value!r}",
            )
        )
    return target


def allowed_step_targets(current: StepStatus, *, step: StepId) -> frozenset[StepStatus]:
    """Statuses ``step`` may move to from ``current``.

    The table is step-agnostic except for one rule: a semantic step may not
    jump from ``VERIFIED_DETERMINISTIC`` straight to ``PROMOTED``.
    """
    targets = _STEP_TRANSITIONS[current]
    if step in SEMANTIC_STEPS and current is StepStatus.VERIFIED_DETERMINISTIC:
        return targets - {StepStatus.PROMOTED}
    return targets


def can_advance_step(current: StepStatus, target: StepStatus, *, step: StepId) -> bool:
    """True when ``step`` may move from ``current`` to ``target``."""
    return target in allowed_step_targets(current, step=step)


def advance_step(current: StepStatus, target: StepStatus, *, step: StepId) -> StepStatus:
    """Validate and perform a step transition."""
    if not can_advance_step(current, target, step=step):
        raise InvalidTransitionError(
            Diagnostic.from_code(
                "STATE_INVALID_TRANSITION",
                f"step {int(step)} may not move from {current.value!r} to {target.value!r}",
            )
        )
    return target
