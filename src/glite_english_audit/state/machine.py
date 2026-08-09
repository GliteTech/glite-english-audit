"""Explicit transition tables for run and stage lifecycles.

Every transition the orchestration may perform is listed here; anything else
raises :class:`InvalidTransitionError` carrying the stable
``STATE_INVALID_TRANSITION`` diagnostic code. Tests enumerate the tables, so a
new transition is a reviewed contract change, not an accident.
"""

from glite_english_audit.artifacts.enums import RunStatus, StageStatus, StepId
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
    # A review the user abandons must still reach EXPIRED, or its stage 4-7
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

_STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.IN_PROGRESS: frozenset(
        {StageStatus.PRODUCED, StageStatus.FAILED, StageStatus.QUARANTINED}
    ),
    StageStatus.PRODUCED: frozenset({StageStatus.VERIFIED_DETERMINISTIC, StageStatus.FAILED}),
    StageStatus.VERIFIED_DETERMINISTIC: frozenset(
        {StageStatus.VERIFIED_SEMANTIC, StageStatus.PROMOTED, StageStatus.FAILED}
    ),
    StageStatus.VERIFIED_SEMANTIC: frozenset({StageStatus.PROMOTED, StageStatus.FAILED}),
    # Promotion is not terminal: replacement after repair re-enters production,
    # and an upstream replacement invalidates this stage (specification, 6.5).
    StageStatus.PROMOTED: frozenset({StageStatus.IN_PROGRESS, StageStatus.INVALIDATED}),
    StageStatus.QUARANTINED: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.FAILED: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.INVALIDATED: frozenset({StageStatus.IN_PROGRESS}),
}

SEMANTIC_STEPS: frozenset[StepId] = frozenset()
"""Steps that may not promote until a second, independent reader has passed.

Empty, and deliberately so. This set exists to forbid the jump from
``VERIFIED_DETERMINISTIC`` to ``PROMOTED``, forcing a ``VERIFIED_SEMANTIC``
step in between. Under the nine-stage design three stages qualified, each with
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


def allowed_stage_targets(current: StageStatus, *, stage: StepId) -> frozenset[StageStatus]:
    """Statuses ``stage`` may move to from ``current``.

    The table is stage-agnostic except for one rule: a semantic stage may not
    jump from ``VERIFIED_DETERMINISTIC`` straight to ``PROMOTED``.
    """
    targets = _STAGE_TRANSITIONS[current]
    if stage in SEMANTIC_STEPS and current is StageStatus.VERIFIED_DETERMINISTIC:
        return targets - {StageStatus.PROMOTED}
    return targets


def can_advance_stage(current: StageStatus, target: StageStatus, *, stage: StepId) -> bool:
    """True when ``stage`` may move from ``current`` to ``target``."""
    return target in allowed_stage_targets(current, stage=stage)


def advance_stage(current: StageStatus, target: StageStatus, *, stage: StepId) -> StageStatus:
    """Validate and perform a stage transition."""
    if not can_advance_stage(current, target, stage=stage):
        raise InvalidTransitionError(
            Diagnostic.from_code(
                "STATE_INVALID_TRANSITION",
                f"stage {int(stage)} may not move from {current.value!r} to {target.value!r}",
            )
        )
    return target
