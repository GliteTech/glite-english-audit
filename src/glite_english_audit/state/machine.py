"""Explicit transition tables for run and stage lifecycles.

Every transition the orchestration may perform is listed here; anything else
raises :class:`InvalidTransitionError` carrying the stable
``STATE_INVALID_TRANSITION`` diagnostic code. Tests enumerate the tables, so a
new transition is a reviewed contract change, not an accident.
"""

from glite_english_audit.artifacts.enums import RunStatus, StageStatus
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
    RunStatus.REVIEW: frozenset({RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS}),
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


def can_advance_stage(current: StageStatus, target: StageStatus) -> bool:
    """True when a stage may move from ``current`` to ``target``."""
    return target in _STAGE_TRANSITIONS[current]


def advance_stage(current: StageStatus, target: StageStatus) -> StageStatus:
    """Validate and perform a stage transition."""
    if not can_advance_stage(current, target):
        raise InvalidTransitionError(
            Diagnostic.from_code(
                "STATE_INVALID_TRANSITION",
                f"stage may not move from {current.value!r} to {target.value!r}",
            )
        )
    return target
