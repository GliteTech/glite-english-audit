"""The manifest must describe what the run has actually done.

The state machine and the manifest store were complete and fully tested, and
no production code called them: every pipeline driver wrote its artifacts and
left the manifest at ``awaiting_preflight`` with all five steps ``pending``.
A real run that had collected 203 utterances, judged authorship on 198, and
written 192 mistake records still reported that nothing had happened.

Resume reads the manifest and not the disk, so that gap meant an interrupted
run would redo finished work, and the review build — which is not a step of
its own and refuses to run until steps a through e are promoted — would have
refused a run whose artifacts were all present.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
)
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, ConsentState
from glite_english_audit.pipeline.record_step import advance_to, enter_review, mark_failed
from glite_english_audit.state.machine import SEMANTIC_STEPS, InvalidTransitionError
from glite_english_audit.state.run_store import create_run, load_manifest, save_manifest

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def run(tmp_path: Path) -> str:
    manifest = create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1", local_scan_confirmed_at=_NOW),
        CompatibilityFingerprint(
            adapter_versions={"claude_code": "1.0.0"},
            artifact_schema_version=1,
            tokenizer_version="1.0.0",
            skill_versions={"find-english-mistakes": 1},
            prompt_versions={"find-mistakes": 1},
            model_ids={"find-mistakes": "example-model-1"},
            consent_policy_version="1",
        ),
        root=tmp_path,
    )
    # A run reaches the drivers already past selection and preflight.
    manifest.status = RunStatus.AWAITING_PREFLIGHT
    save_manifest(manifest, root=tmp_path)
    return manifest.run_id


def test_promoting_a_stage_walks_the_whole_legal_path(run: str, tmp_path: Path) -> None:
    # Asking for PROMOTED from PENDING is legal only as a sequence. If the
    # walk were skipped the state machine would reject the jump, so reaching
    # PROMOTED at all is the proof that each intermediate status was recorded.
    advance_to(run, StepId.A_COLLECTED, StepStatus.PROMOTED, runs_root=tmp_path)
    manifest = load_manifest(run, root=tmp_path)
    assert manifest.steps[StepId.A_COLLECTED].status is StepStatus.PROMOTED


def test_a_semantic_stage_cannot_reach_promoted_without_the_semantic_step(
    run: str, tmp_path: Path
) -> None:
    # The machine forbids VERIFIED_DETERMINISTIC -> PROMOTED for every step
    # that carries model judgment — d and e, since the three old findings and
    # privacy steps collapsed into d — so the walk must include
    # VERIFIED_SEMANTIC. Reaching PROMOTED proves it.
    for step in sorted(SEMANTIC_STEPS, key=int):
        advance_to(run, step, StepStatus.PROMOTED, runs_root=tmp_path)
    manifest = load_manifest(run, root=tmp_path)
    for step in SEMANTIC_STEPS:
        assert manifest.steps[step].status is StepStatus.PROMOTED


def test_the_first_stage_transition_starts_the_run(run: str, tmp_path: Path) -> None:
    assert load_manifest(run, root=tmp_path).status is RunStatus.AWAITING_PREFLIGHT
    advance_to(run, StepId.A_COLLECTED, StepStatus.PRODUCED, runs_root=tmp_path)
    assert load_manifest(run, root=tmp_path).status is RunStatus.PROCESSING


def test_every_transition_checkpoints(run: str, tmp_path: Path) -> None:
    # Specification 9.3: a checkpoint after each step, written only once the
    # artifacts it points at are durable.
    assert load_manifest(run, root=tmp_path).last_checkpoint_at is None
    advance_to(run, StepId.A_COLLECTED, StepStatus.PROMOTED, runs_root=tmp_path, now=_NOW)
    assert load_manifest(run, root=tmp_path).last_checkpoint_at == _NOW


def test_recording_the_same_stage_twice_is_harmless(run: str, tmp_path: Path) -> None:
    # A resumed run reruns the driver for the step it was interrupted in. If
    # the second call raised, resume would be impossible for exactly the runs
    # that need it.
    advance_to(run, StepId.B_DEDUPLICATED, StepStatus.PROMOTED, runs_root=tmp_path)
    advance_to(run, StepId.B_DEDUPLICATED, StepStatus.PROMOTED, runs_root=tmp_path)
    manifest = load_manifest(run, root=tmp_path)
    assert manifest.steps[StepId.B_DEDUPLICATED].status is StepStatus.PROMOTED


def test_the_artifact_pointer_is_recorded_with_the_production_step(
    run: str, tmp_path: Path
) -> None:
    advance_to(
        run,
        StepId.C_AUTHORED,
        StepStatus.PROMOTED,
        artifact_id="artifact-0001",
        artifact_hash="a" * 64,
        producer_version="0.1.0",
        runs_root=tmp_path,
    )
    state = load_manifest(run, root=tmp_path).steps[StepId.C_AUTHORED]
    assert state.current_artifact_id == "artifact-0001"
    assert state.current_artifact_hash == "a" * 64
    assert state.producer_version == "0.1.0"


def test_a_status_off_the_promotion_path_is_refused(run: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="promotion path"):
        advance_to(run, StepId.D_MISTAKES, StepStatus.QUARANTINED, runs_root=tmp_path)


def test_failure_is_recordable_from_pending(run: str, tmp_path: Path) -> None:
    # A step that raised before doing anything must still be nameable as
    # failed, or resume cannot tell it apart from one never attempted.
    mark_failed(run, StepId.D_MISTAKES, runs_root=tmp_path)
    assert load_manifest(run, root=tmp_path).steps[StepId.D_MISTAKES].status is (StepStatus.FAILED)


def test_quarantine_is_distinct_from_failure(run: str, tmp_path: Path) -> None:
    mark_failed(run, StepId.D_MISTAKES, quarantined=True, runs_root=tmp_path)
    assert load_manifest(run, root=tmp_path).steps[StepId.D_MISTAKES].status is (
        StepStatus.QUARANTINED
    )


def test_a_promoted_stage_cannot_be_marked_failed(run: str, tmp_path: Path) -> None:
    # Promotion is not terminal, but the way back is invalidation followed by
    # a fresh production, never a direct edit to failed.
    advance_to(run, StepId.D_MISTAKES, StepStatus.PROMOTED, runs_root=tmp_path)
    with pytest.raises(InvalidTransitionError):
        mark_failed(run, StepId.D_MISTAKES, runs_root=tmp_path)


def test_entering_review_hands_the_run_to_the_user(run: str, tmp_path: Path) -> None:
    enter_review(run, runs_root=tmp_path)
    assert load_manifest(run, root=tmp_path).status is RunStatus.REVIEW


def test_entering_review_twice_does_not_leave_the_review_state(run: str, tmp_path: Path) -> None:
    enter_review(run, runs_root=tmp_path)
    enter_review(run, runs_root=tmp_path)
    assert load_manifest(run, root=tmp_path).status is RunStatus.REVIEW


def test_a_review_cannot_be_built_from_a_partial_run(run: str, tmp_path: Path) -> None:
    """The counts a review shows are the audit's honesty guarantee.

    Computed from a partial run they are still arithmetically consistent and
    still wrong, and nothing downstream can tell the difference. The
    orchestration skill tells the agent to check this; a rule only an agent
    enforces holds until an agent skips a step.
    """
    from glite_english_audit.pipeline.record_step import require_promoted_through

    # The two deterministic steps done, the three judged ones not: every step
    # left unpromoted has to be named, not just the first one missing.
    for step in (StepId.A_COLLECTED, StepId.B_DEDUPLICATED):
        advance_to(run, step, StepStatus.PROMOTED, runs_root=tmp_path)
    with pytest.raises(ValueError, match="steps 2, 3, 4 are not promoted"):
        require_promoted_through(run, StepId.E_VERIFIED, runs_root=tmp_path)


def test_one_unfinished_stage_is_named_in_the_singular(run: str, tmp_path: Path) -> None:
    """A count-driven subject has to agree with its verb in both directions."""
    from glite_english_audit.pipeline.record_step import require_promoted_through

    for step in StepId:
        if int(step) < int(StepId.E_VERIFIED):
            advance_to(run, step, StepStatus.PROMOTED, runs_root=tmp_path)
    with pytest.raises(ValueError, match="step 4 is not promoted"):
        require_promoted_through(run, StepId.E_VERIFIED, runs_root=tmp_path)


def test_a_complete_run_passes_the_same_check(run: str, tmp_path: Path) -> None:
    from glite_english_audit.pipeline.record_step import require_promoted_through

    for step in StepId:
        if int(step) <= int(StepId.E_VERIFIED):
            advance_to(run, step, StepStatus.PROMOTED, runs_root=tmp_path)
    require_promoted_through(run, StepId.E_VERIFIED, runs_root=tmp_path)
