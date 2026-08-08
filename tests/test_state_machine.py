"""Full enumeration of the run and stage lifecycle transition tables."""

import pytest

from glite_english_audit.artifacts.enums import RunStatus, StageStatus
from glite_english_audit.diagnostics.codes import Severity
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
    RunStatus.REVIEW: frozenset({RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS}),
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

_TERMINAL_RUN_STATES = (
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_EXCLUSIONS,
    RunStatus.EXPIRED,
)


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


def test_every_allowed_stage_transition_succeeds() -> None:
    for current, targets in _EXPECTED_STAGE.items():
        for target in targets:
            assert can_advance_stage(current, target)
            assert advance_stage(current, target) is target


def test_every_forbidden_stage_transition_raises() -> None:
    for current in StageStatus:
        for target in StageStatus:
            if target in _EXPECTED_STAGE[current]:
                continue
            assert not can_advance_stage(current, target)
            with pytest.raises(InvalidTransitionError) as exc_info:
                advance_stage(current, target)
            assert exc_info.value.diagnostic.code == "STATE_INVALID_TRANSITION"


@pytest.mark.parametrize("terminal", _TERMINAL_RUN_STATES)
def test_terminal_run_states_have_no_exits(terminal: RunStatus) -> None:
    for target in RunStatus:
        assert not can_advance_run(terminal, target)


def test_self_transitions_are_forbidden() -> None:
    for status in RunStatus:
        assert not can_advance_run(status, status)
    for stage_status in StageStatus:
        assert not can_advance_stage(stage_status, stage_status)


def test_invalid_transition_error_carries_error_diagnostic() -> None:
    with pytest.raises(InvalidTransitionError) as exc_info:
        advance_run(RunStatus.COMPLETED, RunStatus.PROCESSING)
    diagnostic = exc_info.value.diagnostic
    assert diagnostic.code == "STATE_INVALID_TRANSITION"
    assert diagnostic.severity is Severity.ERROR
    assert "completed" in diagnostic.message
    assert "processing" in diagnostic.message
