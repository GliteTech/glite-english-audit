"""Private run store: creation, checkpoints, resume policy, and retention.

One directory per run under the private runtime root holds the manifest, the
per-stage artifacts, logs, and the submission package (specification, 3.6).
Resume behavior is deterministic from the compatibility fingerprint
(specification, 9.4), and retention is state-based: unfinished runs keep their
private artifacts for 30 days after the last successful checkpoint, completed
runs keep only the privacy-safe package and the manifest.

Deletion here is bounded: only the well-known subtrees directly under a run
directory are removed, symlinks are never followed, and the manifest itself is
always kept.
"""

import shutil
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from glite_english_audit import paths
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StageId,
    StageStatus,
)
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.hashing import new_run_id
from glite_english_audit.artifacts.io import ensure_private_dir, read_model, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.state.machine import advance_run, can_advance_run

RUN_MANIFEST_FILENAME = "run-manifest.json"
RETENTION_DAYS = 30
"""Unfinished-run retention after the last checkpoint (specification, 3.6)."""

EARLIEST_SEMANTIC_STAGE = StageId.PLAIN_FINDINGS
"""First stage whose output depends on skills, prompts, or model choice."""

_PRIVATE_SUBDIRS = ("stages", "logs", "submission")
_CLEANUP_ONLY_SUBDIRS = ("stages", "logs")
_CLEANUP_ONLY_FILES = ("selection.json", "progress.json")
_FINISHED_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS, RunStatus.EXPIRED}
)


class RunStoreError(Exception):
    """A run-store operation was refused or failed."""

    def __init__(self, message: str, *, diagnostic: Diagnostic | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class ResumeDecision(StrEnum):
    """Deterministic resume policy outcome (specification, 9.4)."""

    CONTINUE = "continue"
    INVALIDATE_DOWNSTREAM = "invalidate_downstream"
    RESTART = "restart"
    EXPIRED = "expired"


class ResumeAssessment(BaseModel):
    """A resume decision with a short user-facing explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ResumeDecision
    detail: str
    earliest_affected_stage: StageId | None = None


class RunSummary(BaseModel):
    """What the launcher reports about one unfinished run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    started_at: datetime
    status: RunStatus
    last_promoted_stage: StageId | None
    last_checkpoint_at: datetime | None
    checkpoint_age: timedelta


def _base_dir(root: Path | None) -> Path:
    return root if root is not None else paths.runs_root()


def _run_directory(run_id: str, root: Path | None) -> Path:
    return _base_dir(root) / run_id


def _manifest_path(run_id: str, root: Path | None) -> Path:
    return _run_directory(run_id, root) / RUN_MANIFEST_FILENAME


def _last_checkpoint(manifest: RunManifest) -> datetime:
    return manifest.last_checkpoint_at or manifest.created_at


def create_run(
    runtime: AgentRuntime,
    os_environment: OsEnvironment,
    consent: ConsentState,
    fingerprint: CompatibilityFingerprint,
    *,
    root: Path | None = None,
) -> RunManifest:
    """Create a new run directory with its manifest and empty private subdirs."""
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=new_run_id(),
        created_at=utc_now(),
        runtime=runtime,
        os_environment=os_environment,
        status=RunStatus.CREATED,
        consent=consent,
        selection=None,
        stages=empty_stage_map(),
        fingerprint=fingerprint,
        last_checkpoint_at=None,
    )
    run_directory = _run_directory(manifest.run_id, root)
    ensure_private_dir(run_directory)
    for name in _PRIVATE_SUBDIRS:
        ensure_private_dir(run_directory / name)
    write_model(run_directory / RUN_MANIFEST_FILENAME, manifest)
    return manifest


def save_manifest(manifest: RunManifest, *, root: Path | None = None) -> None:
    """Atomically write the manifest into its run directory."""
    write_model(_manifest_path(manifest.run_id, root), manifest)


def load_manifest(run_id: str, *, root: Path | None = None) -> RunManifest:
    """Read and validate the manifest for ``run_id``."""
    path = _manifest_path(run_id, root)
    try:
        return read_model(path, RunManifest)
    except (OSError, ValidationError, ValueError) as error:
        raise RunStoreError(
            f"cannot read run manifest for {run_id!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_CHECKPOINT_CORRUPT",
                f"run manifest is unreadable or invalid: {path.name}",
                item_ref=run_id,
            ),
        ) from error


def _last_promoted_stage(manifest: RunManifest) -> StageId | None:
    promoted = [
        stage for stage, state in manifest.stages.items() if state.status is StageStatus.PROMOTED
    ]
    return max(promoted) if promoted else None


def list_unfinished(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[RunSummary]:
    """Summaries for runs that are neither completed nor expired.

    Unreadable manifests are skipped: a corrupt run cannot be offered for
    resume, and repair is a separate explicit action.
    """
    base = _base_dir(root)
    if not base.is_dir():
        return []
    moment = now if now is not None else utc_now()
    summaries: list[RunSummary] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not (child / RUN_MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = load_manifest(child.name, root=base)
        except RunStoreError:
            continue
        if manifest.status in _FINISHED_STATUSES:
            continue
        summaries.append(
            RunSummary(
                run_id=manifest.run_id,
                started_at=manifest.created_at,
                status=manifest.status,
                last_promoted_stage=_last_promoted_stage(manifest),
                last_checkpoint_at=manifest.last_checkpoint_at,
                checkpoint_age=moment - _last_checkpoint(manifest),
            )
        )
    summaries.sort(key=lambda summary: summary.started_at, reverse=True)
    return summaries


def describe_resume(
    manifest: RunManifest,
    current: CompatibilityFingerprint,
    *,
    now: datetime | None = None,
) -> ResumeAssessment:
    """Apply the deterministic resume policy (specification, 9.4 and 3.6)."""
    moment = now if now is not None else utc_now()
    if moment - _last_checkpoint(manifest) > timedelta(days=RETENTION_DAYS):
        return ResumeAssessment(
            decision=ResumeDecision.EXPIRED,
            detail=(
                f"The last checkpoint is more than {RETENTION_DAYS} days old, so this run "
                "cannot resume. Start a new audit."
            ),
        )

    recorded = manifest.fingerprint
    restart_changes = [
        name
        for name, matches in (
            ("adapter versions", recorded.adapter_versions == current.adapter_versions),
            (
                "artifact schema version",
                recorded.artifact_schema_version == current.artifact_schema_version,
            ),
            ("tokenizer version", recorded.tokenizer_version == current.tokenizer_version),
            (
                "consent policy version",
                recorded.consent_policy_version == current.consent_policy_version,
            ),
        )
        if not matches
    ]
    if restart_changes:
        return ResumeAssessment(
            decision=ResumeDecision.RESTART,
            detail=(
                f"Changed since the checkpoint: {', '.join(restart_changes)}. "
                "Checkpointed artifacts cannot be reused. Start a new run."
            ),
        )

    downstream_changes = [
        name
        for name, matches in (
            ("skill versions", recorded.skill_versions == current.skill_versions),
            ("prompt versions", recorded.prompt_versions == current.prompt_versions),
            ("model ids", recorded.model_ids == current.model_ids),
        )
        if not matches
    ]
    if downstream_changes:
        return ResumeAssessment(
            decision=ResumeDecision.INVALIDATE_DOWNSTREAM,
            detail=(
                f"Changed since the checkpoint: {', '.join(downstream_changes)}. "
                "Findings and later stages are recomputed after a refreshed preflight."
            ),
            earliest_affected_stage=EARLIEST_SEMANTIC_STAGE,
        )

    return ResumeAssessment(
        decision=ResumeDecision.CONTINUE,
        detail="Versions match. Continue from the next incomplete unit.",
    )


def write_checkpoint(
    manifest: RunManifest,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Record a successful checkpoint and atomically save the manifest.

    Callers checkpoint only after the referenced artifacts are durable
    (specification, 9.3).
    """
    manifest.last_checkpoint_at = now if now is not None else utc_now()
    save_manifest(manifest, root=root)
    return manifest


def _refuse_symlinks(run_directory: Path, names: tuple[str, ...]) -> None:
    for name in names:
        if (run_directory / name).is_symlink():
            raise RunStoreError(
                f"refusing cleanup: {name!r} under {run_directory.name!r} is a symlink"
            )


def _delete_subtree(run_directory: Path, name: str) -> None:
    """Delete one well-known subtree directly under the run directory.

    Bounded by construction: the target is a fixed name joined to the run
    directory, symlinked targets are refused, and an escaped resolution
    (a symlinked parent) is refused as well.
    """
    target = run_directory / name
    if target.is_symlink():
        raise RunStoreError(f"refusing cleanup: {name!r} under {run_directory.name!r} is a symlink")
    if not target.exists():
        return
    if target.resolve().parent != run_directory.resolve():
        raise RunStoreError(f"refusing cleanup: {name!r} resolves outside the run directory")
    shutil.rmtree(target)


def _expire_status(status: RunStatus) -> RunStatus | None:
    """Route ``status`` to EXPIRED through the state machine, or None if barred."""
    if can_advance_run(status, RunStatus.EXPIRED):
        return advance_run(status, RunStatus.EXPIRED)
    if can_advance_run(status, RunStatus.CHECKPOINTED) and can_advance_run(
        RunStatus.CHECKPOINTED, RunStatus.EXPIRED
    ):
        checkpointed = advance_run(status, RunStatus.CHECKPOINTED)
        return advance_run(checkpointed, RunStatus.EXPIRED)
    return None


def expire_stale_runs(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Apply the 30-day retention rule to unfinished runs (specification, 3.6).

    Marks each stale manifest EXPIRED via the state machine, then deletes the
    private ``stages/``, ``logs/``, and ``submission/`` subtrees. The manifest
    itself is kept. Returns the expired run IDs.
    """
    base = _base_dir(root)
    if not base.is_dir():
        return []
    moment = now if now is not None else utc_now()
    expired: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not (child / RUN_MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = load_manifest(child.name, root=base)
        except RunStoreError:
            continue
        if manifest.status in _FINISHED_STATUSES:
            continue
        if moment - _last_checkpoint(manifest) <= timedelta(days=RETENTION_DAYS):
            continue
        new_status = _expire_status(manifest.status)
        if new_status is None:
            # REVIEW cannot expire: the run is at final review and only user
            # action finishes it. Retention re-applies once it leaves REVIEW.
            continue
        _refuse_symlinks(child, _PRIVATE_SUBDIRS)
        manifest.status = new_status
        save_manifest(manifest, root=base)
        for name in _PRIVATE_SUBDIRS:
            _delete_subtree(child, name)
        expired.append(manifest.run_id)
    return expired


def cleanup_completed(manifest_dir: Path) -> None:
    """Apply completed-run retention to one run directory (specification, 3.6).

    Keeps only the ``submission/`` package and the run manifest; deletes
    ``stages/``, ``logs/``, and the private selection and progress files.
    Source snapshots live under the repository and are removed by snapshot
    cleanup with its own manifest-bounded safeguards.
    """
    manifest_path = manifest_dir / RUN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RunStoreError(
            f"no run manifest in {manifest_dir.name!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_CHECKPOINT_CORRUPT",
                f"run manifest not found: {manifest_path.name}",
            ),
        )
    manifest = load_manifest(manifest_dir.name, root=manifest_dir.parent)
    if manifest.status not in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS):
        raise RunStoreError(
            f"run {manifest.run_id!r} is {manifest.status.value!r}; "
            "only completed runs are cleaned up"
        )
    _refuse_symlinks(manifest_dir, _CLEANUP_ONLY_SUBDIRS)
    for name in _CLEANUP_ONLY_SUBDIRS:
        _delete_subtree(manifest_dir, name)
    for name in _CLEANUP_ONLY_FILES:
        file_path = manifest_dir / name
        # unlink removes a symlink itself, never its target.
        file_path.unlink(missing_ok=True)
