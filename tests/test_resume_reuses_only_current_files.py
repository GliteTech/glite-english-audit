"""A resume must not reuse the answers the change existed to replace.

``invalidate_from`` edits the manifest and says so in its docstring. Nothing
deleted the files, and both ``--prepare`` commands decided whether to re-ask an
agent by looking at the disk. So a resume after a changed skill, prompt or model
found the old outputs sitting there, reported them as already written, skipped
them, and reported the step recomputed.

The run then attests to a skill version that never judged those records, which is
why this sits beside the attestation tests rather than with the state machine.
"""

from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StageStatus,
    StepId,
)
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION
from glite_english_audit.pipeline.record_stage import output_is_current
from glite_english_audit.state.run_store import (
    RUN_MANIFEST_FILENAME,
    invalidate_from,
    load_manifest,
    save_manifest,
)

_RUN = "run-" + "e" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _seed(runs_root: Path, *, promoted_through: StepId) -> RunManifest:
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=_NOW,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.PROCESSING,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=_NOW,
            provider_transfer_confirmed_at=_NOW,
        ),
        stages=empty_stage_map(),
        fingerprint=CompatibilityFingerprint(
            adapter_versions={},
            artifact_schema_version=MANIFEST_SCHEMA_VERSION,
            tokenizer_version=TOKENIZER_VERSION,
            skill_versions={"find-english-mistakes": 1},
            prompt_versions={},
            model_ids={},
            consent_policy_version=CONSENT_POLICY_VERSION,
        ),
    )
    for step in sorted(manifest.stages):
        if step <= promoted_through:
            manifest.stages[step].status = StageStatus.PROMOTED
    write_model(ensure_private_dir(runs_root / _RUN) / RUN_MANIFEST_FILENAME, manifest)
    return load_manifest(_RUN, root=runs_root)


def test_a_promoted_step_may_reuse_what_is_on_disk(tmp_path: Path) -> None:
    _seed(tmp_path, promoted_through=StepId.C_AUTHORED)
    assert output_is_current(_RUN, StepId.C_AUTHORED, runs_root=tmp_path)


def test_an_invalidated_step_may_not(tmp_path: Path) -> None:
    # The defect: the file was still there, and being there was taken as reason
    # enough to keep it.
    manifest = _seed(tmp_path, promoted_through=StepId.E_VERIFIED)
    invalidate_from(manifest, StepId.C_AUTHORED, now=_NOW)
    save_manifest(manifest, root=tmp_path)
    for step in (StepId.C_AUTHORED, StepId.D_MISTAKES, StepId.E_VERIFIED):
        assert not output_is_current(_RUN, step, runs_root=tmp_path), step


def test_invalidating_one_step_does_not_free_the_steps_before_it(tmp_path: Path) -> None:
    # Invalidation runs downstream only. Steps a and b are script output over
    # unchanged source, so a changed skill is no reason to collect again.
    manifest = _seed(tmp_path, promoted_through=StepId.E_VERIFIED)
    invalidate_from(manifest, StepId.D_MISTAKES, now=_NOW)
    save_manifest(manifest, root=tmp_path)
    assert output_is_current(_RUN, StepId.A_COLLECTED, runs_root=tmp_path)
    assert output_is_current(_RUN, StepId.B_DEDUPLICATED, runs_root=tmp_path)
    assert output_is_current(_RUN, StepId.C_AUTHORED, runs_root=tmp_path)
    assert not output_is_current(_RUN, StepId.D_MISTAKES, runs_root=tmp_path)
    assert not output_is_current(_RUN, StepId.E_VERIFIED, runs_root=tmp_path)


def test_a_step_that_never_ran_has_nothing_stale_to_refuse(tmp_path: Path) -> None:
    # Pending is not invalidated: there is simply no output yet, and the
    # prepare command's own is_file() check answers that question.
    _seed(tmp_path, promoted_through=StepId.A_COLLECTED)
    assert output_is_current(_RUN, StepId.D_MISTAKES, runs_root=tmp_path)
