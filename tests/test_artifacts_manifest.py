"""Run manifest stage-map completeness and the empty stage map.

The map is keyed by :class:`StepId`, so it tracks the five steps and nothing
else: the source inventory, the snapshot manifests and the reviewed submission
are path-derived artifacts now, not steps with a verification lifecycle.
"""

import pytest
from pydantic import ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StageStatus,
    StepId,
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


def _manifest(stages: dict[StepId, StageState], *, run_id: str = "run-" + "0" * 32) -> RunManifest:
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


def test_empty_stage_map_covers_every_step() -> None:
    stages = empty_stage_map()
    assert set(stages) == set(StepId)
    for step, state in stages.items():
        assert state.stage is step
        assert state.status is StageStatus.PENDING
        assert state.current_artifact_id is None
        assert state.current_artifact_hash is None


def test_run_manifest_accepts_complete_stage_map() -> None:
    manifest = _manifest(empty_stage_map())
    assert set(manifest.stages) == set(StepId)
    assert manifest.selection is None
    assert manifest.last_checkpoint_at is None


@pytest.mark.parametrize("missing_step", list(StepId))
def test_run_manifest_rejects_missing_step(missing_step: StepId) -> None:
    stages = empty_stage_map()
    del stages[missing_step]
    with pytest.raises(ValidationError):
        _manifest(stages)


def test_run_manifest_rejects_mismatched_step_key() -> None:
    # The key and the state's own step are two records of the same fact, and
    # promotion repoints whichever the caller happens to read. Letting them
    # disagree would point one step's manifest entry at another step's
    # artifact.
    stages = empty_stage_map()
    stages[StepId.A_COLLECTED] = StageState(stage=StepId.B_DEDUPLICATED)
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
