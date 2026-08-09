"""Run store: create/load, unfinished listing, resume policy, retention."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
)
from glite_english_audit.artifacts.manifest import (
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
)
from glite_english_audit.state.machine import advance_run, advance_step
from glite_english_audit.state.run_store import (
    RETENTION_DAYS,
    RUN_MANIFEST_FILENAME,
    ResumeDecision,
    RunStoreError,
    cleanup_completed,
    create_run,
    describe_resume,
    expire_stale_runs,
    invalidate_from,
    list_unfinished,
    load_manifest,
    next_incomplete_step,
    save_manifest,
    write_checkpoint,
)

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _fingerprint(**overrides: object) -> CompatibilityFingerprint:
    base: dict[str, object] = {
        "adapter_versions": {"claude_code": "1.0.0", "codex": "1.0.0"},
        "artifact_schema_version": 1,
        "tokenizer_version": "1.0.0",
        "skill_versions": {"find-english-mistakes": 1},
        "prompt_versions": {"find-mistakes": 1},
        "model_ids": {"find-mistakes": "example-model-1"},
        "consent_policy_version": "1",
    }
    base.update(overrides)
    return CompatibilityFingerprint.model_validate(base)


def _create(root: Path, fingerprint: CompatibilityFingerprint | None = None) -> RunManifest:
    return create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1"),
        fingerprint if fingerprint is not None else _fingerprint(),
        root=root,
    )


def _advance(manifest: RunManifest, *targets: RunStatus) -> None:
    for target in targets:
        manifest.status = advance_run(manifest.status, target)


_TO_PROCESSING = (RunStatus.SELECTING, RunStatus.AWAITING_PREFLIGHT, RunStatus.PROCESSING)
_TO_COMPLETED = (*_TO_PROCESSING, RunStatus.REVIEW, RunStatus.COMPLETED)


def test_create_writes_manifest_and_private_dirs(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    run_directory = tmp_path / manifest.run_id
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()
    for name in ("steps", "logs", "snapshot-manifests", "submission"):
        assert (run_directory / name).is_dir()
    assert manifest.status is RunStatus.CREATED
    assert manifest.last_checkpoint_at is None
    assert all(state.status is StepStatus.PENDING for state in manifest.steps.values())


def test_create_load_round_trip(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    loaded = load_manifest(manifest.run_id, root=tmp_path)
    assert loaded == manifest


def test_load_missing_manifest_raises_with_diagnostic(tmp_path: Path) -> None:
    with pytest.raises(RunStoreError) as excinfo:
        load_manifest("run-" + "e" * 32, root=tmp_path)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize("run_id", ["run-does-not-exist", "../../victim", "/absolute", ""])
def test_load_rejects_malformed_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(RunStoreError) as excinfo:
        load_manifest(run_id, root=tmp_path)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_RUN_ID_INVALID"


def test_load_rejects_manifest_belonging_to_another_run(tmp_path: Path) -> None:
    original = _create(tmp_path)
    copy_id = "run-" + "a" * 32
    shutil.copytree(tmp_path / original.run_id, tmp_path / copy_id)

    with pytest.raises(RunStoreError) as excinfo:
        load_manifest(copy_id, root=tmp_path)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_RUN_DIRECTORY_MISMATCH"


def test_load_corrupt_manifest_raises(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    (tmp_path / manifest.run_id / RUN_MANIFEST_FILENAME).write_text("{not json", "utf-8")
    with pytest.raises(RunStoreError):
        load_manifest(manifest.run_id, root=tmp_path)


def test_list_unfinished_skips_finished_runs(tmp_path: Path) -> None:
    unfinished = _create(tmp_path)
    finished = _create(tmp_path)
    _advance(finished, *_TO_COMPLETED)
    save_manifest(finished, root=tmp_path)

    summaries = list_unfinished(tmp_path, now=_NOW)
    assert [summary.run_id for summary in summaries] == [unfinished.run_id]
    assert summaries[0].status is RunStatus.CREATED


def test_list_unfinished_reports_promotion_and_checkpoint_age(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    stage_state = manifest.steps[StepId.A_COLLECTED]
    for target in (
        StepStatus.IN_PROGRESS,
        StepStatus.PRODUCED,
        StepStatus.VERIFIED_DETERMINISTIC,
        StepStatus.PROMOTED,
    ):
        stage_state.status = advance_step(stage_state.status, target, step=StepId.A_COLLECTED)
    checkpoint_at = _NOW - timedelta(hours=6)
    write_checkpoint(manifest, root=tmp_path, now=checkpoint_at)

    (summary,) = list_unfinished(tmp_path, now=_NOW)
    assert summary.last_promoted_step is StepId.A_COLLECTED
    assert summary.last_checkpoint_at == checkpoint_at
    assert summary.checkpoint_age == timedelta(hours=6)


def test_list_unfinished_empty_root(tmp_path: Path) -> None:
    assert list_unfinished(tmp_path / "missing") == []


def test_write_checkpoint_updates_and_persists_timestamp(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    assert manifest.last_checkpoint_at is None
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assert manifest.last_checkpoint_at == _NOW
    assert load_manifest(manifest.run_id, root=tmp_path).last_checkpoint_at == _NOW


def test_resume_continue_when_fingerprints_match(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.decision is ResumeDecision.CONTINUE
    assert assessment.earliest_affected_step is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"skill_versions": {"find-english-mistakes": 2}},
        {"prompt_versions": {"find-mistakes": 2}},
        {"model_ids": {"find-mistakes": "example-model-2"}},
    ],
)
def test_resume_invalidates_downstream_from_the_first_agent_step(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    # Skills, prompts and model IDs are compared as whole maps, not per step, so
    # a change in any of them lands on the earliest step whose file content is
    # model judgment. That is step c: steps a and b are collected and
    # deduplicated by script, so their per-session files stay reusable, while c
    # and everything after it are recomputed.
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assessment = describe_resume(manifest, _fingerprint(**overrides), now=_NOW)
    assert assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert assessment.earliest_affected_step is StepId.C_AUTHORED


@pytest.mark.parametrize(
    "overrides",
    [
        {"adapter_versions": {"claude_code": "2.0.0", "codex": "1.0.0"}},
        {"artifact_schema_version": 2},
        {"tokenizer_version": "2.0.0"},
        {"consent_policy_version": "2"},
    ],
)
def test_resume_restart_on_incompatible_change(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assessment = describe_resume(manifest, _fingerprint(**overrides), now=_NOW)
    assert assessment.decision is ResumeDecision.RESTART


def test_resume_invalidates_from_the_mistakes_step_on_client_version_change(
    tmp_path: Path,
) -> None:
    # A pure-Python change (for example a privacy-scanner fix) must not let a
    # run keep the step-d mistake files, or the step-e verification of them,
    # that the known-bad scanner approved (specification, 6.6).
    manifest = _create(tmp_path, _fingerprint(client_version="0.0.1"))
    write_checkpoint(manifest, root=tmp_path, now=_NOW)

    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert assessment.earliest_affected_step is StepId.D_MISTAKES


def test_resume_records_the_running_client_version_by_default() -> None:
    assert _fingerprint().client_version == CLIENT_VERSION


def test_resume_client_change_never_widens_an_earlier_invalidation(tmp_path: Path) -> None:
    # Two mismatches at once land on the earlier of the two boundaries. The
    # client-code boundary is step d and the model-judgment boundary is step c,
    # so a client change on top of a skill change must add nothing: the run
    # whose client version also changed stops exactly where the one whose did
    # not stops.
    changed_skill = _fingerprint(skill_versions={"find-english-mistakes": 2})
    also_changed_client = _create(tmp_path, _fingerprint(client_version="0.0.1"))
    write_checkpoint(also_changed_client, root=tmp_path, now=_NOW)
    skill_change_only = _create(tmp_path)
    write_checkpoint(skill_change_only, root=tmp_path, now=_NOW)

    both = describe_resume(also_changed_client, changed_skill, now=_NOW)
    skill_alone = describe_resume(skill_change_only, changed_skill, now=_NOW)

    assert both.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert both.earliest_affected_step is StepId.C_AUTHORED
    assert both.earliest_affected_step == skill_alone.earliest_affected_step


def test_resume_continues_at_twenty_nine_days(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW - timedelta(days=29))
    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.decision is ResumeDecision.CONTINUE


def test_resume_expired_at_thirty_one_days(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW - timedelta(days=31))
    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.decision is ResumeDecision.EXPIRED


def test_resume_expiry_wins_over_fingerprint_mismatch(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW - timedelta(days=31))
    assessment = describe_resume(manifest, _fingerprint(tokenizer_version="2.0.0"), now=_NOW)
    assert assessment.decision is ResumeDecision.EXPIRED


def test_expire_stale_runs_thirty_day_boundary(tmp_path: Path) -> None:
    fresh = _create(tmp_path)
    write_checkpoint(fresh, root=tmp_path, now=_NOW - timedelta(days=29))
    stale = _create(tmp_path)
    write_checkpoint(stale, root=tmp_path, now=_NOW - timedelta(days=31))
    (tmp_path / stale.run_id / "steps" / "private.json").write_text("{}", "utf-8")
    # The manifests list the source files a snapshot copied, so they are private
    # too and moved out of the step tree to the run root in the same change.
    (tmp_path / stale.run_id / "snapshot-manifests" / "claude_code.json").write_text("{}", "utf-8")

    assert expire_stale_runs(tmp_path, now=_NOW) == [stale.run_id]

    stale_dir = tmp_path / stale.run_id
    assert load_manifest(stale.run_id, root=tmp_path).status is RunStatus.EXPIRED
    assert (stale_dir / RUN_MANIFEST_FILENAME).is_file()
    assert not (stale_dir / "steps").exists()
    assert not (stale_dir / "logs").exists()
    assert not (stale_dir / "snapshot-manifests").exists()
    assert not (stale_dir / "submission").exists()

    fresh_dir = tmp_path / fresh.run_id
    assert load_manifest(fresh.run_id, root=tmp_path).status is RunStatus.CREATED
    assert (fresh_dir / "steps").is_dir()


def test_expire_routes_processing_through_checkpointed(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_PROCESSING)
    manifest.last_checkpoint_at = _NOW - timedelta(days=31)
    save_manifest(manifest, root=tmp_path)

    assert expire_stale_runs(tmp_path, now=_NOW) == [manifest.run_id]
    assert load_manifest(manifest.run_id, root=tmp_path).status is RunStatus.EXPIRED


def test_expire_stale_runs_expires_an_abandoned_review(tmp_path: Path) -> None:
    # A user who closes the review tab leaves the run in REVIEW with step d and
    # step e files containing raw source language. Retention must still apply.
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_PROCESSING, RunStatus.REVIEW)
    manifest.last_checkpoint_at = _NOW - timedelta(days=400)
    save_manifest(manifest, root=tmp_path)
    run_directory = tmp_path / manifest.run_id
    (run_directory / "steps" / "findings.md").write_text("private source language", "utf-8")

    assert expire_stale_runs(tmp_path, now=_NOW) == [manifest.run_id]
    assert load_manifest(manifest.run_id, root=tmp_path).status is RunStatus.EXPIRED
    assert not (run_directory / "steps").exists()
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()


def test_expire_stale_runs_ignores_a_copied_run_directory(tmp_path: Path) -> None:
    # A restored backup or copied directory must never expire the run whose ID
    # its manifest carries: that would leave the original unresumable with its
    # private artifacts still on disk.
    original = _create(tmp_path)
    write_checkpoint(original, root=tmp_path, now=_NOW - timedelta(days=1))
    original_directory = tmp_path / original.run_id
    (original_directory / "steps" / "private.json").write_text("{}", "utf-8")

    copy_directory = tmp_path / ("run-" + "a" * 32)
    shutil.copytree(original_directory, copy_directory)
    stale = load_manifest(original.run_id, root=tmp_path)
    stale.last_checkpoint_at = _NOW - timedelta(days=31)
    (copy_directory / RUN_MANIFEST_FILENAME).write_text(
        stale.model_dump_json(indent=2), encoding="utf-8"
    )

    assert expire_stale_runs(tmp_path, now=_NOW) == []
    assert load_manifest(original.run_id, root=tmp_path).status is RunStatus.CREATED
    assert (original_directory / "steps" / "private.json").is_file()


def test_list_unfinished_skips_a_copied_run_directory(tmp_path: Path) -> None:
    original = _create(tmp_path)
    shutil.copytree(tmp_path / original.run_id, tmp_path / ("run-" + "a" * 32))

    summaries = list_unfinished(tmp_path, now=_NOW)
    assert [summary.run_id for summary in summaries] == [original.run_id]


def test_expire_refuses_symlinked_steps_dir(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", "utf-8")

    root = tmp_path / "runs"
    root.mkdir()
    manifest = _create(root)
    write_checkpoint(manifest, root=root, now=_NOW - timedelta(days=31))
    steps = root / manifest.run_id / "steps"
    steps.rmdir()
    steps.symlink_to(victim)

    with pytest.raises(RunStoreError, match="symlink"):
        expire_stale_runs(root, now=_NOW)
    assert (victim / "keep.txt").exists()


def test_cleanup_completed_keeps_package_and_manifest(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_COMPLETED)
    save_manifest(manifest, root=tmp_path)
    run_directory = tmp_path / manifest.run_id
    (run_directory / "steps" / "private.json").write_text("{}", "utf-8")
    (run_directory / "logs" / "run.log").write_text("log", "utf-8")
    (run_directory / "snapshot-manifests" / "claude_code.json").write_text("{}", "utf-8")
    (run_directory / "selection.json").write_text("{}", "utf-8")
    (run_directory / "progress.json").write_text("{}", "utf-8")
    # The inventory names the applications and paths found on this machine, so
    # it leaves with the rest once the package is built.
    (run_directory / "source-inventory.json").write_text("{}", "utf-8")
    (run_directory / "submission" / "package.json").write_text("{}", "utf-8")

    cleanup_completed(run_directory)

    assert not (run_directory / "steps").exists()
    assert not (run_directory / "logs").exists()
    assert not (run_directory / "snapshot-manifests").exists()
    assert not (run_directory / "selection.json").exists()
    assert not (run_directory / "progress.json").exists()
    assert not (run_directory / "source-inventory.json").exists()
    assert (run_directory / "submission" / "package.json").is_file()
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()


def test_cleanup_refuses_unfinished_run(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    with pytest.raises(RunStoreError, match="only completed runs"):
        cleanup_completed(tmp_path / manifest.run_id)


def test_cleanup_refuses_symlinked_steps_dir(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", "utf-8")

    root = tmp_path / "runs"
    root.mkdir()
    manifest = _create(root)
    _advance(manifest, *_TO_COMPLETED)
    save_manifest(manifest, root=root)
    steps = root / manifest.run_id / "steps"
    steps.rmdir()
    steps.symlink_to(victim)

    with pytest.raises(RunStoreError, match="symlink"):
        cleanup_completed(root / manifest.run_id)
    assert (victim / "keep.txt").exists()


# -- snapshots are private data too ------------------------------------------


def _leftover_snapshot(run_directory: Path) -> Path:
    """A snapshot an interrupted extraction left behind.

    Manifest-bounded cleanup removes snapshots as soon as extraction is
    durable, so a snapshot still on disk at retention time means extraction
    never finished. It holds a verbatim copy of the user's application data.
    """
    copied = run_directory / "snapshots" / "claude_code" / "abc123def456" / "session.jsonl"
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_text('{"text": "private source language"}\n', "utf-8")
    return copied


def test_expiry_deletes_a_snapshot_an_interrupted_extraction_left(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW - timedelta(days=31))
    copied = _leftover_snapshot(tmp_path / manifest.run_id)

    assert expire_stale_runs(tmp_path, now=_NOW) == [manifest.run_id]

    assert not copied.exists()
    assert not (tmp_path / manifest.run_id / "snapshots").exists()


def test_completed_cleanup_deletes_a_snapshot_an_interrupted_extraction_left(
    tmp_path: Path,
) -> None:
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_COMPLETED)
    save_manifest(manifest, root=tmp_path)
    run_directory = tmp_path / manifest.run_id
    copied = _leftover_snapshot(run_directory)

    cleanup_completed(run_directory)

    assert not copied.exists()
    assert not (run_directory / "snapshots").exists()
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()


def test_create_makes_the_snapshot_directory_with_the_others(tmp_path: Path) -> None:
    # Retention walks a fixed list of subtrees; a snapshot directory created
    # somewhere else at extraction time would fall outside it.
    manifest = _create(tmp_path)
    assert (tmp_path / manifest.run_id / "snapshots").is_dir()


# -- resume pointer and downstream invalidation ------------------------------


def _promote(manifest: RunManifest, *steps: StepId) -> None:
    """Walk each named step to PROMOTED the way the pipeline promotes it.

    Straight from the deterministic check, with no semantic verification in
    between: no step has an independent second reader any more
    (``SEMANTIC_STEPS`` is empty), and steps a and b are scripts, for which the
    status would mean nothing.
    """
    for step in steps:
        state = manifest.steps[step]
        for target in (
            StepStatus.IN_PROGRESS,
            StepStatus.PRODUCED,
            StepStatus.VERIFIED_DETERMINISTIC,
            StepStatus.PROMOTED,
        ):
            state.status = advance_step(state.status, target, step=step)
        state.current_artifact_id = f"art-{int(step)}"
        state.current_artifact_hash = f"{int(step):064d}"


def test_next_incomplete_stage_is_the_first_unpromoted_one(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    assert next_incomplete_step(manifest) is StepId.A_COLLECTED

    _promote(manifest, StepId.A_COLLECTED, StepId.B_DEDUPLICATED)
    assert next_incomplete_step(manifest) is StepId.C_AUTHORED


def test_next_incomplete_stage_ignores_promotions_above_a_gap(tmp_path: Path) -> None:
    # Step d is promoted while step c is not, so the mistakes rest on authored
    # spans that are missing. Resuming at d would analyze nothing.
    manifest = _create(tmp_path)
    _promote(manifest, StepId.A_COLLECTED, StepId.B_DEDUPLICATED, StepId.D_MISTAKES)
    assert next_incomplete_step(manifest) is StepId.C_AUTHORED


def test_next_incomplete_stage_is_none_when_every_stage_is_promoted(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _promote(manifest, *StepId)
    assert next_incomplete_step(manifest) is None


def test_invalidate_from_clears_the_stage_and_everything_after_it(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _promote(manifest, *StepId)

    invalidated = invalidate_from(manifest, StepId.D_MISTAKES, now=_NOW)

    assert invalidated == [StepId.D_MISTAKES, StepId.E_VERIFIED]
    for step in invalidated:
        state = manifest.steps[step]
        assert state.status is StepStatus.INVALIDATED
        # The pointer goes with the status: a step that must be recomputed may
        # not stay the manifest's current artifact for lineage checks.
        assert state.current_artifact_id is None
        assert state.current_artifact_hash is None
        assert state.updated_at == _NOW
    # Everything upstream keeps its promoted artifact.
    for step in (StepId.A_COLLECTED, StepId.B_DEDUPLICATED, StepId.C_AUTHORED):
        assert manifest.steps[step].status is StepStatus.PROMOTED
        assert manifest.steps[step].current_artifact_id == f"art-{int(step)}"


def test_invalidate_from_leaves_a_failed_stage_and_its_history_alone(tmp_path: Path) -> None:
    # The failed step sits inside the invalidated range, not at its edge: only
    # a promoted step is cleared, so the diagnostic history of the step that
    # failed survives the resume decision.
    manifest = _create(tmp_path)
    _promote(manifest, *StepId)
    failed = manifest.steps[StepId.E_VERIFIED]
    failed.status = advance_step(failed.status, StepStatus.IN_PROGRESS, step=StepId.E_VERIFIED)
    failed.status = advance_step(failed.status, StepStatus.FAILED, step=StepId.E_VERIFIED)

    invalidated = invalidate_from(manifest, StepId.D_MISTAKES, now=_NOW)

    assert invalidated == [StepId.D_MISTAKES]
    assert StepId.E_VERIFIED not in invalidated
    assert manifest.steps[StepId.E_VERIFIED].status is StepStatus.FAILED


def test_invalidate_from_the_first_stage_invalidates_the_whole_run(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _promote(manifest, *StepId)

    assert invalidate_from(manifest, StepId.A_COLLECTED, now=_NOW) == list(StepId)


def test_a_refused_resume_carries_the_code_the_documents_promise(tmp_path: Path) -> None:
    """README's troubleshooting section names these codes to the user.

    Both were registered, documented in specifications/diagnostic_codes.md, and
    emitted by nothing: describe_resume returned prose only. A code a user is
    told to look for and that no code path can produce is a documentation
    promise the software does not keep.
    """
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW - timedelta(days=RETENTION_DAYS + 1))

    expired = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert expired.decision is ResumeDecision.EXPIRED
    assert expired.diagnostic is not None
    assert expired.diagnostic.code == "STATE_EXPIRED_INPUT"

    fresh = _create(tmp_path)
    write_checkpoint(fresh, root=tmp_path, now=_NOW)
    incompatible = describe_resume(fresh, _fingerprint(tokenizer_version="9.9.9"), now=_NOW)
    assert incompatible.decision is ResumeDecision.RESTART
    assert incompatible.diagnostic is not None
    assert incompatible.diagnostic.code == "STATE_RESUME_INCOMPATIBLE"


def test_a_resumable_run_carries_no_refusal_code(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.diagnostic is None
