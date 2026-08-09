"""Full enumeration of the run and stage lifecycle transition tables."""

import pytest

from glite_english_audit.artifacts.enums import RunStatus, StageStatus, StepId
from glite_english_audit.diagnostics.codes import Severity
from glite_english_audit.state import machine
from glite_english_audit.state.machine import (
    InvalidTransitionError,
    advance_run,
    advance_stage,
    can_advance_run,
    can_advance_stage,
)

# Expected tables mirrored from the contract. A change in the state machine
# must be matched by a reviewed change here.
_EXPECTED_RUN: dict[RunStatus, frozenset[RunStatus]] = {
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
    # An abandoned review must still fall under the 30-day retention rule, and
    # a user who reopens the review re-enters processing (specification, 3.6).
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

_EXPECTED_STAGE: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.IN_PROGRESS: frozenset(
        {StageStatus.PRODUCED, StageStatus.FAILED, StageStatus.QUARANTINED}
    ),
    StageStatus.PRODUCED: frozenset({StageStatus.VERIFIED_DETERMINISTIC, StageStatus.FAILED}),
    StageStatus.VERIFIED_DETERMINISTIC: frozenset(
        {StageStatus.VERIFIED_SEMANTIC, StageStatus.PROMOTED, StageStatus.FAILED}
    ),
    StageStatus.VERIFIED_SEMANTIC: frozenset({StageStatus.PROMOTED, StageStatus.FAILED}),
    StageStatus.PROMOTED: frozenset({StageStatus.IN_PROGRESS, StageStatus.INVALIDATED}),
    StageStatus.QUARANTINED: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.FAILED: frozenset({StageStatus.IN_PROGRESS}),
    StageStatus.INVALIDATED: frozenset({StageStatus.IN_PROGRESS}),
}

# No step waits on a second, independent reader before promotion, so every
# step promotes on the deterministic verifier alone. Step c is checked
# deterministically and quarantines what fails, step d's independent findings
# verifier was removed on purpose, and step e is itself the second reader and
# cannot wait on itself (see state/machine.py for the full reasoning). Mirrored
# empty so that listing a step here again is a reviewed contract change.
_EXPECTED_SEMANTIC_STEPS: frozenset[StepId] = frozenset()
_DETERMINISTIC_STEPS = frozenset(StepId) - _EXPECTED_SEMANTIC_STEPS

_TERMINAL_RUN_STATES = (
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_EXCLUSIONS,
    RunStatus.EXPIRED,
)


def _expected_stage_targets(step: StepId, current: StageStatus) -> frozenset[StageStatus]:
    targets = _EXPECTED_STAGE[current]
    if step in _EXPECTED_SEMANTIC_STEPS and current is StageStatus.VERIFIED_DETERMINISTIC:
        return targets - {StageStatus.PROMOTED}
    return targets


def test_expected_tables_cover_every_state() -> None:
    assert set(_EXPECTED_RUN) == set(RunStatus)
    assert set(_EXPECTED_STAGE) == set(StageStatus)


def test_every_allowed_run_transition_succeeds() -> None:
    for current, targets in _EXPECTED_RUN.items():
        for target in targets:
            assert can_advance_run(current, target)
            assert advance_run(current, target) is target


def test_every_forbidden_run_transition_raises() -> None:
    for current in RunStatus:
        for target in RunStatus:
            if target in _EXPECTED_RUN[current]:
                continue
            assert not can_advance_run(current, target)
            with pytest.raises(InvalidTransitionError) as exc_info:
                advance_run(current, target)
            assert exc_info.value.diagnostic.code == "STATE_INVALID_TRANSITION"


@pytest.mark.parametrize("step", list(StepId))
def test_every_allowed_stage_transition_succeeds(step: StepId) -> None:
    for current in StageStatus:
        for target in _expected_stage_targets(step, current):
            assert can_advance_stage(current, target, stage=step)
            assert advance_stage(current, target, stage=step) is target


@pytest.mark.parametrize("step", list(StepId))
def test_every_forbidden_stage_transition_raises(step: StepId) -> None:
    for current in StageStatus:
        allowed = _expected_stage_targets(step, current)
        for target in StageStatus:
            if target in allowed:
                continue
            assert not can_advance_stage(current, target, stage=step)
            with pytest.raises(InvalidTransitionError) as exc_info:
                advance_stage(current, target, stage=step)
            assert exc_info.value.diagnostic.code == "STATE_INVALID_TRANSITION"


def test_no_step_waits_on_a_second_reader_before_promotion() -> None:
    # The empty set is the contract, not an oversight: none of the five steps
    # has an independent semantic verifier to wait for. Re-listing a step here
    # reinstates the VERIFIED_SEMANTIC gate below, so it must be deliberate.
    assert machine.SEMANTIC_STEPS == _EXPECTED_SEMANTIC_STEPS


def test_a_step_listed_as_semantic_may_not_promote_on_the_deterministic_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate is unreachable while SEMANTIC_STEPS is empty, so listing a step
    # is the only way to exercise it. It is kept in the machine for the day a
    # verifier is added back; an untested gate would rot before then.
    monkeypatch.setattr(machine, "SEMANTIC_STEPS", frozenset({StepId.D_MISTAKES}))
    assert not can_advance_stage(
        StageStatus.VERIFIED_DETERMINISTIC, StageStatus.PROMOTED, stage=StepId.D_MISTAKES
    )
    with pytest.raises(InvalidTransitionError) as exc_info:
        advance_stage(
            StageStatus.VERIFIED_DETERMINISTIC, StageStatus.PROMOTED, stage=StepId.D_MISTAKES
        )
    assert exc_info.value.diagnostic.code == "STATE_INVALID_TRANSITION"
    assert can_advance_stage(
        StageStatus.VERIFIED_DETERMINISTIC, StageStatus.VERIFIED_SEMANTIC, stage=StepId.D_MISTAKES
    )
    assert (
        advance_stage(StageStatus.VERIFIED_SEMANTIC, StageStatus.PROMOTED, stage=StepId.D_MISTAKES)
        is StageStatus.PROMOTED
    )
    # The gate binds the listed step only; an unlisted step keeps its shortcut.
    assert can_advance_stage(
        StageStatus.VERIFIED_DETERMINISTIC, StageStatus.PROMOTED, stage=StepId.C_AUTHORED
    )


@pytest.mark.parametrize("step", sorted(_DETERMINISTIC_STEPS))
def test_deterministic_step_may_promote_after_the_deterministic_verifier(
    step: StepId,
) -> None:
    assert can_advance_stage(StageStatus.VERIFIED_DETERMINISTIC, StageStatus.PROMOTED, stage=step)
    assert (
        advance_stage(StageStatus.VERIFIED_DETERMINISTIC, StageStatus.PROMOTED, stage=step)
        is StageStatus.PROMOTED
    )


@pytest.mark.parametrize("terminal", _TERMINAL_RUN_STATES)
def test_terminal_run_states_have_no_exits(terminal: RunStatus) -> None:
    for target in RunStatus:
        assert not can_advance_run(terminal, target)


def test_review_can_expire_and_reopen() -> None:
    assert can_advance_run(RunStatus.REVIEW, RunStatus.EXPIRED)
    assert can_advance_run(RunStatus.REVIEW, RunStatus.PROCESSING)


def test_self_transitions_are_forbidden() -> None:
    for status in RunStatus:
        assert not can_advance_run(status, status)
    for stage_status in StageStatus:
        for step in StepId:
            assert not can_advance_stage(stage_status, stage_status, stage=step)


def test_invalid_transition_error_carries_error_diagnostic() -> None:
    with pytest.raises(InvalidTransitionError) as exc_info:
        advance_run(RunStatus.COMPLETED, RunStatus.PROCESSING)
    diagnostic = exc_info.value.diagnostic
    assert diagnostic.code == "STATE_INVALID_TRANSITION"
    assert diagnostic.severity is Severity.ERROR
    assert "completed" in diagnostic.message
    assert "processing" in diagnostic.message
