"""Run manifest: consent, selection, per-stage state, and compatibility.

The manifest points to exactly one current artifact per stage. There is no
revision chain: replacing an artifact repoints the manifest and invalidates
downstream stages (specification, 5.2, 6.5).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StageStatus,
    StepId,
)
from glite_english_audit.paths import validate_run_id

MANIFEST_SCHEMA_VERSION = 1


class ConsentState(BaseModel):
    """Which consents were given, with their policy versions and times.

    Local-scan consent may be remembered across runs until the consent version
    changes. Provider-transfer consent is per-run and never inferred from a
    prior audit (specification, 2.2).
    """

    model_config = ConfigDict(extra="forbid")

    consent_policy_version: str
    local_scan_confirmed_at: datetime | None = None
    provider_transfer_confirmed_at: datetime | None = None
    preflight_confirmed_at: datetime | None = None
    adult_confirmed_at: datetime | None = None
    """Moment 4a: the user attested to being 18 or older before sending."""
    storage_terms_confirmed_at: datetime | None = None
    """Moment 4b: the user accepted permanent, irrevocable storage, the
    disclosed uses, and external AI processing of the records they send.

    Kept separate from the age attestation because the review page presents
    them as two unchecked boxes and either may be given without the other.
    Collapsing them into one timestamp would record an agreement the user
    never made as a single act."""


class PeriodSelection(BaseModel):
    """The audited period, resolved to concrete UTC dates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: str
    start: datetime
    end: datetime


class SelectionState(BaseModel):
    """What the user selected. Instance keys are local; labels are opaque."""

    model_config = ConfigDict(extra="forbid")

    selected_instance_keys: list[str]
    excluded_instance_keys: list[str] = Field(default_factory=list)
    period: PeriodSelection
    processing_profile: str
    record_cutoff_at: datetime
    """Record-level source cutoff frozen when selection is confirmed.

    Records created after this moment belong to the next audit and never
    invalidate resume (specification, 13.5).
    """


class CompatibilityFingerprint(BaseModel):
    """Everything a checkpoint depends on. Any mismatch drives resume policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_versions: dict[str, str]
    artifact_schema_version: int
    tokenizer_version: str
    skill_versions: dict[str, int]
    prompt_versions: dict[str, int]
    model_ids: dict[str, str]
    consent_policy_version: str
    client_version: str = CLIENT_VERSION
    """Version of this package.

    Without it a checkpoint survives any pure-Python change, so a run
    checkpointed before a privacy-scanner fix would resume with records the
    known-bad scanner approved (specification, 6.6).
    """


class StageState(BaseModel):
    """Verification lifecycle and current artifact pointer for one stage."""

    model_config = ConfigDict(extra="forbid")

    stage: StepId
    status: StageStatus = StageStatus.PENDING
    current_artifact_id: str | None = None
    current_artifact_hash: str | None = None
    producer_version: str | None = None
    updated_at: datetime | None = None


class RunManifest(BaseModel):
    """The single authoritative state file for one audit run."""

    model_config = ConfigDict(extra="forbid")

    manifest_schema_version: int = Field(ge=1)
    run_id: str
    created_at: datetime
    runtime: AgentRuntime
    os_environment: OsEnvironment
    status: RunStatus
    consent: ConsentState
    selection: SelectionState | None = None
    stages: dict[StepId, StageState]
    fingerprint: CompatibilityFingerprint
    last_checkpoint_at: datetime | None = None

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        # The run ID names the private run directory and the repository-owned
        # snapshot directory, so it may never be absolute or traversing.
        return validate_run_id(value)

    @field_validator("stages")
    @classmethod
    def _complete_stage_map(cls, value: dict[StepId, StageState]) -> dict[StepId, StageState]:
        missing = [stage for stage in StepId if stage not in value]
        if missing:
            msg = f"manifest must track every stage; missing: {missing}"
            raise ValueError(msg)
        for stage, state in value.items():
            if state.stage is not stage:
                msg = f"stage state under key {stage} claims to be {state.stage}"
                raise ValueError(msg)
        return value


def empty_stage_map() -> dict[StepId, StageState]:
    """A fresh all-pending stage map for a new run."""
    return {stage: StageState(stage=stage) for stage in StepId}
