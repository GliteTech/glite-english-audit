"""Run manifest stage-map completeness and the empty stage map."""

import pytest
from pydantic import ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StageId,
    StageStatus,
)
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    StageState,
    empty_stage_map,
)


def _fingerprint() -> CompatibilityFingerprint:
    return CompatibilityFingerprint(
        adapter_versions={"claude_code": "1.0.0"},
        artifact_schema_version=1,
        tokenizer_version="1.0.0",
        skill_versions={"run-english-audit": 1},
        prompt_versions={},
        model_ids={},
        consent_policy_version="2026-01",
    )


def _manifest(stages: dict[StageId, StageState], *, run_id: str = "run-" + "0" * 32) -> RunManifest:
    return RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        created_at=utc_now(),
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.CREATED,
        consent=ConsentState(consent_policy_version="2026-01"),
        stages=stages,
        fingerprint=_fingerprint(),
    )


def test_empty_stage_map_covers_all_stages() -> None:
    stages = empty_stage_map()
    assert set(stages) == set(StageId)
    for stage, state in stages.items():
        assert state.stage is stage
        assert state.status is StageStatus.PENDING
        assert state.current_artifact_id is None
        assert state.current_artifact_hash is None


def test_run_manifest_accepts_complete_stage_map() -> None:
    manifest = _manifest(empty_stage_map())
    assert set(manifest.stages) == set(StageId)
    assert manifest.selection is None
    assert manifest.last_checkpoint_at is None


@pytest.mark.parametrize("missing_stage", list(StageId))
def test_run_manifest_rejects_missing_stage(missing_stage: StageId) -> None:
    stages = empty_stage_map()
    del stages[missing_stage]
    with pytest.raises(ValidationError):
        _manifest(stages)


def test_run_manifest_rejects_mismatched_stage_key() -> None:
    stages = empty_stage_map()
    stages[StageId.SOURCE_INVENTORY] = StageState(stage=StageId.SOURCE_SNAPSHOTS)
    with pytest.raises(ValidationError):
        _manifest(stages)


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "run-does-not-exist",
        "run-" + "0" * 31,
        "RUN-" + "0" * 32,
        "../../victim",
        "/absolute",
        "run-" + "0" * 32 + "/../..",
    ],
)
def test_run_manifest_rejects_malformed_run_id(run_id: str) -> None:
    # The run ID is joined into filesystem paths, so a manifest may never carry
    # an absolute or traversing value.
    with pytest.raises(ValidationError):
        _manifest(empty_stage_map(), run_id=run_id)


def test_fingerprint_records_the_running_client_version() -> None:
    assert _fingerprint().client_version == CLIENT_VERSION


def test_fingerprint_keeps_a_recorded_client_version() -> None:
    recorded = CompatibilityFingerprint(
        adapter_versions={},
        artifact_schema_version=1,
        tokenizer_version="1.0.0",
        skill_versions={},
        prompt_versions={},
        model_ids={},
        consent_policy_version="2026-01",
        client_version="0.0.1",
    )
    assert recorded.client_version == "0.0.1"
