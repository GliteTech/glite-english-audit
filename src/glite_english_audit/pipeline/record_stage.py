"""Record stage progress in the run manifest, and checkpoint it.

The state machine in :mod:`glite_english_audit.state.machine` and the manifest
store in :mod:`glite_english_audit.state.run_store` were complete and tested,
and nothing in the pipeline called them. Every driver wrote its artifacts and
left the manifest untouched, so a run that had collected, judged authorship,
and produced findings still reported ``awaiting_preflight`` with all nine
stages ``pending``.

That is not a cosmetic gap. Resume reads the manifest, not the disk, so an
interrupted run would redo work it had already finished; stage 8 refuses to
build a review unless stages 0-7 are promoted, so the manifest would have
blocked a run whose artifacts were all present; and specification 9.3 requires
a checkpoint after each stage, which nothing was writing.

Ordering rule, from specification 9.3: checkpoint only after the artifacts the
manifest points at are durable. Every function here is therefore called after
the write, never before, and the manifest is saved atomically by the store.

Advancing runs through the intermediate statuses rather than jumping is
deliberate. ``advance_stage`` rejects a jump, and that rejection is the check
that a driver did not skip verification.
"""

from datetime import datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import RunStatus, StageId, StageStatus
from glite_english_audit.artifacts.manifest import RunManifest
from glite_english_audit.state.machine import SEMANTIC_STAGES, advance_run, advance_stage
from glite_english_audit.state.run_store import load_manifest, write_checkpoint

# The path a deterministic stage walks from pending to promoted. A semantic
# stage inserts VERIFIED_SEMANTIC before promotion, which the machine enforces.
_DETERMINISTIC_PATH: tuple[StageStatus, ...] = (
    StageStatus.IN_PROGRESS,
    StageStatus.PRODUCED,
    StageStatus.VERIFIED_DETERMINISTIC,
    StageStatus.PROMOTED,
)

_SEMANTIC_PATH: tuple[StageStatus, ...] = (
    StageStatus.IN_PROGRESS,
    StageStatus.PRODUCED,
    StageStatus.VERIFIED_DETERMINISTIC,
    StageStatus.VERIFIED_SEMANTIC,
    StageStatus.PROMOTED,
)


def _path_for(stage: StageId) -> tuple[StageStatus, ...]:
    return _SEMANTIC_PATH if stage in SEMANTIC_STAGES else _DETERMINISTIC_PATH


def _begin_processing(manifest: RunManifest) -> None:
    """Move the run into processing when a stage starts doing work.

    A run sitting at ``awaiting_preflight`` or ``checkpointed`` is between
    stages; the first stage transition of a driver is what makes it running
    again. A run already processing stays as it is.
    """
    if manifest.status in (RunStatus.AWAITING_PREFLIGHT, RunStatus.CHECKPOINTED):
        manifest.status = advance_run(manifest.status, RunStatus.PROCESSING)


def advance_to(
    run_id: str,
    stage: StageId,
    target: StageStatus,
    *,
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
    producer_version: str | None = None,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Walk ``stage`` forward to ``target`` and checkpoint the manifest.

    Intermediate statuses are stepped through in order, so a caller asking for
    ``PROMOTED`` from ``PENDING`` performs the whole legal sequence rather than
    an illegal jump. A stage already at ``target`` is left alone, which makes
    this safe to call again after an interrupted run resumes.

    ``artifact_id``, ``artifact_hash``, and ``producer_version`` describe the
    artifact the stage now points at. They are recorded once the stage reaches
    ``PRODUCED``, because before that there is nothing to point at.
    """
    manifest = load_manifest(run_id, root=runs_root)
    state = manifest.stages[stage]
    path = _path_for(stage)

    if state.status is target:
        return manifest
    if target not in path:
        # Failure and quarantine are recorded by `mark_failed`, which does not
        # walk a path: they are reachable from wherever the stage stands.
        msg = f"{target.value!r} is not on the promotion path for stage {int(stage)}"
        raise ValueError(msg)

    _begin_processing(manifest)

    remaining = path[path.index(state.status) + 1 :] if state.status in path else path
    for step in remaining:
        state.status = advance_stage(state.status, step, stage=stage)
        if step is StageStatus.PRODUCED:
            state.current_artifact_id = artifact_id
            state.current_artifact_hash = artifact_hash
            state.producer_version = producer_version
        if step is target:
            break

    state.updated_at = now
    manifest.stages[stage] = state
    return write_checkpoint(manifest, root=runs_root, now=now)


def enter_review(
    run_id: str,
    *,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Move the run to ``review``: every local stage is done, a person is not.

    This is the boundary the whole design turns on. Up to here the run may act
    on its own; past here nothing leaves the machine until the user says so.
    """
    manifest = load_manifest(run_id, root=runs_root)
    if manifest.status is not RunStatus.REVIEW:
        _begin_processing(manifest)
        manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    return write_checkpoint(manifest, root=runs_root, now=now)


def mark_failed(
    run_id: str,
    stage: StageId,
    *,
    quarantined: bool = False,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Record that a stage failed, so resume repairs it instead of trusting it.

    A stage that raised and left its status at ``in_progress`` is
    indistinguishable from one still running. Naming the failure is what lets
    the resume logic offer a repair rather than a redo.
    """
    manifest = load_manifest(run_id, root=runs_root)
    state = manifest.stages[stage]
    target = StageStatus.QUARANTINED if quarantined else StageStatus.FAILED
    if state.status is StageStatus.PENDING:
        state.status = advance_stage(state.status, StageStatus.IN_PROGRESS, stage=stage)
    if state.status is not target:
        state.status = advance_stage(state.status, target, stage=stage)
    state.updated_at = now
    manifest.stages[stage] = state
    return write_checkpoint(manifest, root=runs_root, now=now)
