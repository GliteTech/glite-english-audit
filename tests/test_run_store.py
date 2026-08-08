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
    StageId,
    StageStatus,
)
from glite_english_audit.artifacts.manifest import (
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
)
from glite_english_audit.state.machine import advance_run, advance_stage
from glite_english_audit.state.run_store import (
    RUN_MANIFEST_FILENAME,
    ResumeDecision,
    RunStoreError,
    cleanup_completed,
    create_run,
    describe_resume,
    expire_stale_runs,
    list_unfinished,
    load_manifest,
    save_manifest,
    write_checkpoint,
)

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _fingerprint(**overrides: object) -> CompatibilityFingerprint:
    base: dict[str, object] = {
        "adapter_versions": {"claude_code": "1.0.0", "codex": "1.0.0"},
        "artifact_schema_version": 1,
        "tokenizer_version": "1.0.0",
        "skill_versions": {"analyze-english-text": 1},
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
    for name in ("stages", "logs", "submission"):
        assert (run_directory / name).is_dir()
    assert manifest.status is RunStatus.CREATED
    assert manifest.last_checkpoint_at is None
    assert all(state.status is StageStatus.PENDING for state in manifest.stages.values())


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
    stage_state = manifest.stages[StageId.SOURCE_INVENTORY]
    for target in (
        StageStatus.IN_PROGRESS,
        StageStatus.PRODUCED,
        StageStatus.VERIFIED_DETERMINISTIC,
        StageStatus.PROMOTED,
    ):
        stage_state.status = advance_stage(
            stage_state.status, target, stage=StageId.SOURCE_INVENTORY
        )
    checkpoint_at = _NOW - timedelta(hours=6)
    write_checkpoint(manifest, root=tmp_path, now=checkpoint_at)

    (summary,) = list_unfinished(tmp_path, now=_NOW)
    assert summary.last_promoted_stage is StageId.SOURCE_INVENTORY
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
    assert assessment.earliest_affected_stage is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"skill_versions": {"analyze-english-text": 2}},
        {"prompt_versions": {"find-mistakes": 2}},
        {"model_ids": {"find-mistakes": "example-model-2"}},
    ],
)
def test_resume_invalidates_downstream_from_stage_four(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    manifest = _create(tmp_path)
    write_checkpoint(manifest, root=tmp_path, now=_NOW)
    assessment = describe_resume(manifest, _fingerprint(**overrides), now=_NOW)
    assert assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert assessment.earliest_affected_stage is StageId.PLAIN_FINDINGS
    assert int(StageId.PLAIN_FINDINGS) == 4


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


def test_resume_invalidates_from_safe_records_on_client_version_change(tmp_path: Path) -> None:
    # A pure-Python change (for example a privacy-scanner fix) must not let a
    # run keep SAFE_RECORDS and PRIVACY_APPROVED artifacts approved by the
    # known-bad scanner (specification, 6.6).
    manifest = _create(tmp_path, _fingerprint(client_version="0.0.1"))
    write_checkpoint(manifest, root=tmp_path, now=_NOW)

    assessment = describe_resume(manifest, _fingerprint(), now=_NOW)
    assert assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert assessment.earliest_affected_stage is StageId.SAFE_RECORDS


def test_resume_records_the_running_client_version_by_default() -> None:
    assert _fingerprint().client_version == CLIENT_VERSION


def test_resume_client_change_never_widens_an_earlier_invalidation(tmp_path: Path) -> None:
    manifest = _create(tmp_path, _fingerprint(client_version="0.0.1"))
    write_checkpoint(manifest, root=tmp_path, now=_NOW)

    assessment = describe_resume(
        manifest, _fingerprint(skill_versions={"analyze-english-text": 2}), now=_NOW
    )
    assert assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert assessment.earliest_affected_stage is StageId.PLAIN_FINDINGS


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
    (tmp_path / stale.run_id / "stages" / "private.json").write_text("{}", "utf-8")

    assert expire_stale_runs(tmp_path, now=_NOW) == [stale.run_id]

    stale_dir = tmp_path / stale.run_id
    assert load_manifest(stale.run_id, root=tmp_path).status is RunStatus.EXPIRED
    assert (stale_dir / RUN_MANIFEST_FILENAME).is_file()
    assert not (stale_dir / "stages").exists()
    assert not (stale_dir / "logs").exists()
    assert not (stale_dir / "submission").exists()

    fresh_dir = tmp_path / fresh.run_id
    assert load_manifest(fresh.run_id, root=tmp_path).status is RunStatus.CREATED
    assert (fresh_dir / "stages").is_dir()


def test_expire_routes_processing_through_checkpointed(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_PROCESSING)
    manifest.last_checkpoint_at = _NOW - timedelta(days=31)
    save_manifest(manifest, root=tmp_path)

    assert expire_stale_runs(tmp_path, now=_NOW) == [manifest.run_id]
    assert load_manifest(manifest.run_id, root=tmp_path).status is RunStatus.EXPIRED


def test_expire_stale_runs_expires_an_abandoned_review(tmp_path: Path) -> None:
    # A user who closes the review tab leaves the run in REVIEW with stage 4-7
    # artifacts containing raw source language. Retention must still apply.
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_PROCESSING, RunStatus.REVIEW)
    manifest.last_checkpoint_at = _NOW - timedelta(days=400)
    save_manifest(manifest, root=tmp_path)
    run_directory = tmp_path / manifest.run_id
    (run_directory / "stages" / "findings.md").write_text("private source language", "utf-8")

    assert expire_stale_runs(tmp_path, now=_NOW) == [manifest.run_id]
    assert load_manifest(manifest.run_id, root=tmp_path).status is RunStatus.EXPIRED
    assert not (run_directory / "stages").exists()
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()


def test_expire_stale_runs_ignores_a_copied_run_directory(tmp_path: Path) -> None:
    # A restored backup or copied directory must never expire the run whose ID
    # its manifest carries: that would leave the original unresumable with its
    # private artifacts still on disk.
    original = _create(tmp_path)
    write_checkpoint(original, root=tmp_path, now=_NOW - timedelta(days=1))
    original_directory = tmp_path / original.run_id
    (original_directory / "stages" / "private.json").write_text("{}", "utf-8")

    copy_directory = tmp_path / ("run-" + "a" * 32)
    shutil.copytree(original_directory, copy_directory)
    stale = load_manifest(original.run_id, root=tmp_path)
    stale.last_checkpoint_at = _NOW - timedelta(days=31)
    (copy_directory / RUN_MANIFEST_FILENAME).write_text(
        stale.model_dump_json(indent=2), encoding="utf-8"
    )

    assert expire_stale_runs(tmp_path, now=_NOW) == []
    assert load_manifest(original.run_id, root=tmp_path).status is RunStatus.CREATED
    assert (original_directory / "stages" / "private.json").is_file()


def test_list_unfinished_skips_a_copied_run_directory(tmp_path: Path) -> None:
    original = _create(tmp_path)
    shutil.copytree(tmp_path / original.run_id, tmp_path / ("run-" + "a" * 32))

    summaries = list_unfinished(tmp_path, now=_NOW)
    assert [summary.run_id for summary in summaries] == [original.run_id]


def test_expire_refuses_symlinked_stages_dir(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", "utf-8")

    root = tmp_path / "runs"
    root.mkdir()
    manifest = _create(root)
    write_checkpoint(manifest, root=root, now=_NOW - timedelta(days=31))
    stages = root / manifest.run_id / "stages"
    stages.rmdir()
    stages.symlink_to(victim)

    with pytest.raises(RunStoreError, match="symlink"):
        expire_stale_runs(root, now=_NOW)
    assert (victim / "keep.txt").exists()


def test_cleanup_completed_keeps_package_and_manifest(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    _advance(manifest, *_TO_COMPLETED)
    save_manifest(manifest, root=tmp_path)
    run_directory = tmp_path / manifest.run_id
    (run_directory / "stages" / "private.json").write_text("{}", "utf-8")
    (run_directory / "logs" / "run.log").write_text("log", "utf-8")
    (run_directory / "selection.json").write_text("{}", "utf-8")
    (run_directory / "progress.json").write_text("{}", "utf-8")
    (run_directory / "submission" / "package.json").write_text("{}", "utf-8")

    cleanup_completed(run_directory)

    assert not (run_directory / "stages").exists()
    assert not (run_directory / "logs").exists()
    assert not (run_directory / "selection.json").exists()
    assert not (run_directory / "progress.json").exists()
    assert (run_directory / "submission" / "package.json").is_file()
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()


def test_cleanup_refuses_unfinished_run(tmp_path: Path) -> None:
    manifest = _create(tmp_path)
    with pytest.raises(RunStoreError, match="only completed runs"):
        cleanup_completed(tmp_path / manifest.run_id)


def test_cleanup_refuses_symlinked_stages_dir(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", "utf-8")

    root = tmp_path / "runs"
    root.mkdir()
    manifest = _create(root)
    _advance(manifest, *_TO_COMPLETED)
    save_manifest(manifest, root=root)
    stages = root / manifest.run_id / "stages"
    stages.rmdir()
    stages.symlink_to(victim)

    with pytest.raises(RunStoreError, match="symlink"):
        cleanup_completed(root / manifest.run_id)
    assert (victim / "keep.txt").exists()
