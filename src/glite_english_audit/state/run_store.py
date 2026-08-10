"""Private run store: creation, checkpoints, resume policy, and retention.

One directory per run under the private runtime root holds the manifest, the
per-step artifacts, logs, and the submission package (specification, 3.6).
Resume behavior is deterministic from the compatibility fingerprint
(specification, 9.4), and retention is state-based: unfinished runs keep their
private artifacts for 30 days after the last successful checkpoint, completed
runs keep only the privacy-safe package and the manifest.

Deletion here is bounded: only the well-known subtrees directly under a run
directory are removed, symlinks are never followed, and the manifest itself is
always kept.
"""

import shutil
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from glite_english_audit import paths
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
)
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.hashing import new_run_id
from glite_english_audit.artifacts.io import ensure_private_dir, read_model, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_step_map,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.state.machine import advance_run, advance_step, can_advance_run

RUN_MANIFEST_FILENAME = "run-manifest.json"
RETENTION_DAYS = 30
"""Unfinished-run retention after the last checkpoint (specification, 3.6)."""

EARLIEST_SEMANTIC_STEP = StepId.C_AUTHORED
"""First step whose output depends on skills, prompts, or model choice."""

EARLIEST_CLIENT_CODE_STEP = StepId.D_MISTAKES
"""First step a pure-Python change invalidates.

Step d depends on the deterministic privacy scanner and the packaging
allowlist, so a client change may not leave records approved by the previous
code promoted (specification, 6.6, 8.3).
"""

_PRIVATE_SUBDIRS = ("steps", "logs", "snapshots", "snapshot-manifests", "submission")
"""Private subtrees of a run directory, created together and expired together.

``steps/`` holds the learner's own sentences at every step, and named
``steps/`` until the five-step refactor — a rename that left retention
pointing at an empty directory while every file it was meant to delete sat in
the new one. ``snapshot-manifests/`` and ``source-inventory.json`` moved to the
run root in the same change and were outside retention entirely; the inventory
names local applications and paths, and the manifests list copied source files.

``snapshots/`` holds verbatim copies of the user's own application data
(:mod:`glite_english_audit.discovery.snapshot_safety`). Manifest-bounded
snapshot cleanup removes them as soon as extraction is durable, but an
interrupted or failed extraction leaves them behind, so retention must reach
them too (specification, 3.6).
"""

_CLEANUP_ONLY_SUBDIRS = ("steps", "logs", "snapshots", "snapshot-manifests")
_CLEANUP_ONLY_FILES = ("selection.json", "progress.json", "source-inventory.json")
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
    earliest_affected_step: StepId | None = None
    diagnostic: Diagnostic | None = None
    """The stable code for this refusal, when there is one.

    ``detail`` is what a person reads and may be reworded freely. The code is
    what the README, the troubleshooting docs, and any future log line refer
    to, and both documents named codes that nothing emitted until this field
    existed."""


class RunSummary(BaseModel):
    """What the launcher reports about one unfinished run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    started_at: datetime
    status: RunStatus
    last_promoted_step: StepId | None
    last_checkpoint_at: datetime | None
    checkpoint_age: timedelta


def _base_dir(root: Path | None) -> Path:
    return root if root is not None else paths.runs_root()


def _run_directory(run_id: str, root: Path | None) -> Path:
    try:
        validated = paths.validate_run_id(run_id)
    except ValueError as error:
        raise RunStoreError(
            f"not a valid run identifier: {run_id!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_RUN_ID_INVALID",
                "run identifier does not match the required form",
            ),
        ) from error
    return _base_dir(root) / validated


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
        steps=empty_step_map(),
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
    _save_manifest_in(_run_directory(manifest.run_id, root), manifest)


def _save_manifest_in(directory: Path, manifest: RunManifest) -> None:
    """Write ``manifest`` into ``directory`` regardless of the ID it carries.

    Retention and cleanup address a run by the directory they are iterating, so
    they must never write through the ID a copied or restored manifest claims.
    """
    write_model(directory / RUN_MANIFEST_FILENAME, manifest)


def _load_manifest_in(directory: Path) -> RunManifest:
    """Read the manifest stored in ``directory`` and confirm it owns it.

    A copied or restored run directory holds a manifest naming the original
    run. Accepting it would let a sweep over the copy write through to the
    original, so the mismatch is refused here and the copy is left untouched
    for an explicit repair.
    """
    path = directory / RUN_MANIFEST_FILENAME
    try:
        manifest = read_model(path, RunManifest)
    except (OSError, ValidationError, ValueError) as error:
        raise RunStoreError(
            f"cannot read run manifest in {directory.name!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_CHECKPOINT_CORRUPT",
                f"run manifest is unreadable or invalid: {path.name}",
                item_ref=directory.name,
            ),
        ) from error
    if manifest.run_id != directory.name:
        raise RunStoreError(
            f"run directory {directory.name!r} holds a manifest for {manifest.run_id!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_RUN_DIRECTORY_MISMATCH",
                "run directory name and manifest run ID disagree",
                item_ref=directory.name,
            ),
        )
    return manifest


def load_manifest(run_id: str, *, root: Path | None = None) -> RunManifest:
    """Read and validate the manifest for ``run_id``."""
    return _load_manifest_in(_run_directory(run_id, root))


def _last_promoted_step(manifest: RunManifest) -> StepId | None:
    promoted = [
        step for step, state in manifest.steps.items() if state.status is StepStatus.PROMOTED
    ]
    return max(promoted) if promoted else None


def list_unfinished(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[RunSummary]:
    """Summaries for runs that are neither completed nor expired.

    Unreadable manifests, symlinks, and directories whose name disagrees with
    their manifest are skipped: none can be offered for resume, and repair is a
    separate explicit action.
    """
    base = _base_dir(root)
    if not base.is_dir():
        return []
    moment = now if now is not None else utc_now()
    summaries: list[RunSummary] = []
    for child in sorted(base.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if not (child / RUN_MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = _load_manifest_in(child)
        except RunStoreError:
            continue
        if manifest.status in _FINISHED_STATUSES:
            continue
        summaries.append(
            RunSummary(
                run_id=manifest.run_id,
                started_at=manifest.created_at,
                status=manifest.status,
                last_promoted_step=_last_promoted_step(manifest),
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
    if manifest.status in _FINISHED_STATUSES:
        # Both endings delete the run's private artifacts: completion by
        # `cleanup_completed`, expiry by `expire_stale_runs`. Nothing is left
        # to continue from, whatever the clock says.
        reason = (
            f"It expired under the {RETENTION_DAYS}-day rule."
            if manifest.status is RunStatus.EXPIRED
            else "It already finished."
        )
        return ResumeAssessment(
            decision=ResumeDecision.EXPIRED,
            detail=(
                f"This run cannot resume. {reason} Its private artifacts were deleted. "
                "Start a new audit."
            ),
            diagnostic=Diagnostic.from_code(
                "STATE_EXPIRED_INPUT",
                "the run's private artifacts were deleted when it ended",
                item_ref=manifest.run_id,
            ),
        )
    if moment - _last_checkpoint(manifest) > timedelta(days=RETENTION_DAYS):
        return ResumeAssessment(
            decision=ResumeDecision.EXPIRED,
            detail=(
                f"The last checkpoint is more than {RETENTION_DAYS} days old, so this run "
                "cannot resume. Start a new audit."
            ),
            diagnostic=Diagnostic.from_code(
                "STATE_EXPIRED_INPUT",
                f"the last checkpoint passed the {RETENTION_DAYS}-day retention limit",
                item_ref=manifest.run_id,
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
            diagnostic=Diagnostic.from_code(
                "STATE_RESUME_INCOMPATIBLE",
                f"the checkpoint fingerprint differs in: {', '.join(restart_changes)}",
                item_ref=manifest.run_id,
            ),
        )

    downstream_changes = [
        (name, step)
        for name, step, matches in (
            (
                "skill versions",
                EARLIEST_SEMANTIC_STEP,
                recorded.skill_versions == current.skill_versions,
            ),
            (
                "prompt versions",
                EARLIEST_SEMANTIC_STEP,
                recorded.prompt_versions == current.prompt_versions,
            ),
            # The model the session was observed running, not one anything
            # chose: the semantic steps inherit it, so a different one now
            # means the reusable work was judged by a different reader.
            ("session model", EARLIEST_SEMANTIC_STEP, recorded.model_ids == current.model_ids),
            (
                "client version",
                EARLIEST_CLIENT_CODE_STEP,
                recorded.client_version == current.client_version,
            ),
        )
        if not matches
    ]
    if downstream_changes:
        earliest = min(step for _, step in downstream_changes)
        names = [name for name, _ in downstream_changes]
        return ResumeAssessment(
            decision=ResumeDecision.INVALIDATE_DOWNSTREAM,
            detail=(
                f"Changed since the checkpoint: {', '.join(names)}. "
                f"Step {paths.step_dir_name(earliest)[0]} and later are recomputed "
                "after a refreshed preflight."
            ),
            earliest_affected_step=earliest,
        )

    return ResumeAssessment(
        decision=ResumeDecision.CONTINUE,
        detail="Versions match. Continue from the next incomplete unit.",
    )


def next_incomplete_step(manifest: RunManifest) -> StepId | None:
    """The earliest step that is not promoted, or ``None`` when all are.

    Where a continued run resumes (specification, 9.4). A step below a
    promoted one counts as incomplete too: its output is missing, so anything
    later rests on nothing and has to be recomputed after it.
    """
    for step in sorted(manifest.steps):
        if manifest.steps[step].status is not StepStatus.PROMOTED:
            return step
    return None


def invalidate_from(
    manifest: RunManifest,
    earliest: StepId,
    *,
    now: datetime | None = None,
) -> list[StepId]:
    """Invalidate ``earliest`` and every promoted step after it.

    The invalidate-downstream branch of the resume policy (specification, 9.4,
    6.5). Only promoted steps move: a quarantined or failed step keeps its
    status, so its diagnostic history survives the decision. The artifact
    pointer is cleared with the status, because a step that must be recomputed
    may not stay the manifest's current artifact for lineage checks.

    Callers save the manifest; this function only edits it.
    """
    moment = now if now is not None else utc_now()
    invalidated: list[StepId] = []
    for step in sorted(manifest.steps):
        state = manifest.steps[step]
        if step < earliest or state.status is not StepStatus.PROMOTED:
            continue
        state.status = advance_step(state.status, StepStatus.INVALIDATED, step=step)
        state.current_artifact_id = None
        state.current_artifact_hash = None
        state.updated_at = moment
        invalidated.append(step)
    return invalidated


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


def _unsafe_cleanup_path(message: str, *, item_ref: str) -> RunStoreError:
    return RunStoreError(
        f"refusing cleanup: {message}",
        diagnostic=Diagnostic.from_code("STATE_UNSAFE_CLEANUP_PATH", message, item_ref=item_ref),
    )


def _refuse_symlinked_run_directory(run_directory: Path) -> None:
    """Refuse a run directory that is itself a symlink.

    Every containment check below resolves the run directory first, so a link
    named like a run would make its target look contained and put an arbitrary
    directory — a source application store, a home directory — one fixed name
    away from deletion. This store only ever creates real directories, so a
    link here is never ours.
    """
    if run_directory.is_symlink():
        raise _unsafe_cleanup_path(
            f"run directory {run_directory.name!r} is a symlink", item_ref=run_directory.name
        )


def _refuse_symlinks(run_directory: Path, names: tuple[str, ...]) -> None:
    _refuse_symlinked_run_directory(run_directory)
    for name in names:
        if (run_directory / name).is_symlink():
            raise _unsafe_cleanup_path(
                f"{name!r} under {run_directory.name!r} is a symlink", item_ref=run_directory.name
            )


def _delete_subtree(run_directory: Path, name: str) -> None:
    """Delete one well-known subtree directly under the run directory.

    Bounded by construction: the run directory and the target are both refused
    when they are symlinks, the target is a fixed name joined to the run
    directory, and an escaped resolution is refused as well.
    """
    _refuse_symlinked_run_directory(run_directory)
    target = run_directory / name
    if target.is_symlink():
        raise _unsafe_cleanup_path(
            f"{name!r} under {run_directory.name!r} is a symlink", item_ref=run_directory.name
        )
    if not target.exists():
        return
    if target.resolve().parent != run_directory.resolve():
        raise _unsafe_cleanup_path(
            f"{name!r} resolves outside the run directory", item_ref=run_directory.name
        )
    shutil.rmtree(target)


def _expire_status(status: RunStatus) -> RunStatus | None:
    """Route ``status`` to EXPIRED through the state machine, or None if barred.

    Every unfinished status can reach EXPIRED, directly or through
    CHECKPOINTED; None is left for a status the tables later close off.
    """
    if can_advance_run(status, RunStatus.EXPIRED):
        return advance_run(status, RunStatus.EXPIRED)
    if can_advance_run(status, RunStatus.CHECKPOINTED) and can_advance_run(
        RunStatus.CHECKPOINTED, RunStatus.EXPIRED
    ):
        checkpointed = advance_run(status, RunStatus.CHECKPOINTED)
        return advance_run(checkpointed, RunStatus.EXPIRED)
    return None


def _expire_unreadable(child: Path, moment: datetime) -> None:
    """Sweep a run whose manifest this version cannot validate.

    Retention is a promise about text on disk, so it cannot depend on the
    manifest parsing. The last checkpoint is unreadable here, so the newest
    private file stands in for it: whatever wrote last is the most recent this
    run can have been touched. The manifest is left in place, as it is for every
    other expiry, so a person can still identify the directory.
    """
    newest = 0.0
    for name in _PRIVATE_SUBDIRS:
        subtree = child / name
        if not subtree.is_dir() or subtree.is_symlink():
            continue
        for path in subtree.rglob("*"):
            if path.is_file() and not path.is_symlink():
                newest = max(newest, path.stat().st_mtime)
    # The root-level files are private too, and a run can hold them with every
    # private subtree already gone. Dating the run from the subtrees alone made
    # `newest` zero for exactly that run and returned before deleting anything,
    # so `source-inventory.json` — path hashes and labels — outlived retention
    # in the branch added to stop things outliving retention.
    for name in _CLEANUP_ONLY_FILES:
        candidate = child / name
        if candidate.is_file() and not candidate.is_symlink():
            newest = max(newest, candidate.stat().st_mtime)
    if newest == 0.0:
        return
    if moment - datetime.fromtimestamp(newest, tz=UTC) <= timedelta(days=RETENTION_DAYS):
        return
    _refuse_symlinks(child, _PRIVATE_SUBDIRS)
    for name in _PRIVATE_SUBDIRS:
        _delete_subtree(child, name)
    for name in _CLEANUP_ONLY_FILES:
        (child / name).unlink(missing_ok=True)


def expire_stale_runs(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Apply the 30-day retention rule to unfinished runs (specification, 3.6).

    Marks each stale manifest EXPIRED via the state machine, then deletes every
    private subtree, snapshots included, and the private run-root files. Only
    the manifest is kept. Returns the expired run IDs.

    Each directory is read and written through itself. A directory whose name
    disagrees with its manifest is left alone: writing through the claimed ID
    would expire a different, possibly live run. A symlink is skipped for the
    same reason it is refused during deletion.
    """
    base = _base_dir(root)
    if not base.is_dir():
        return []
    moment = now if now is not None else utc_now()
    expired: list[str] = []
    for child in sorted(base.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if not (child / RUN_MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = _load_manifest_in(child)
        except RunStoreError:
            # A manifest this version cannot read still sits beside the text it
            # describes. Skipping it was how a run written by an older schema
            # outlived retention entirely: never offered for resume, because the
            # launcher skips what it cannot parse, and never swept, because this
            # loop did too — so its extracted sentences stayed on disk for good.
            # Unreadable is a reason to sweep, not to spare.
            _expire_unreadable(child, moment)
            continue
        if manifest.status in _FINISHED_STATUSES:
            continue
        if moment - _last_checkpoint(manifest) <= timedelta(days=RETENTION_DAYS):
            continue
        new_status = _expire_status(manifest.status)
        if new_status is None:
            continue
        _refuse_symlinks(child, _PRIVATE_SUBDIRS)
        manifest.status = new_status
        _save_manifest_in(child, manifest)
        for name in _PRIVATE_SUBDIRS:
            _delete_subtree(child, name)
        for name in _CLEANUP_ONLY_FILES:
            # Expiry deleted only subtrees, so the run-root files outlived the
            # retention rule. source-inventory.json names the user's local
            # applications and the absolute paths they store data under, which
            # is the most locating thing a finished run can leave behind.
            (child / name).unlink(missing_ok=True)
        expired.append(manifest.run_id)
    return expired


def cleanup_completed(manifest_dir: Path) -> None:
    """Apply completed-run retention to one run directory (specification, 3.6).

    Keeps only the ``submission/`` package and the run manifest; deletes
    ``steps/``, ``logs/``, the snapshot manifests, any snapshot left behind by
    a source whose extraction failed, and the private run-root files including
    the source inventory.
    """
    _refuse_symlinked_run_directory(manifest_dir)
    manifest_path = manifest_dir / RUN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RunStoreError(
            f"no run manifest in {manifest_dir.name!r}",
            diagnostic=Diagnostic.from_code(
                "STATE_CHECKPOINT_CORRUPT",
                f"run manifest not found: {manifest_path.name}",
            ),
        )
    manifest = _load_manifest_in(manifest_dir)
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
