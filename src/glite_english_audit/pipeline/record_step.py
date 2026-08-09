"""Record step progress in the run manifest, and checkpoint it.

The state machine in :mod:`glite_english_audit.state.machine` and the manifest
store in :mod:`glite_english_audit.state.run_store` were complete and tested,
and nothing in the pipeline called them. Every driver wrote its artifacts and
left the manifest untouched, so a run that had collected, judged authorship,
and produced findings still reported ``awaiting_preflight`` with all nine
steps ``pending``.

That is not a cosmetic gap. Resume reads the manifest, not the disk, so an
interrupted run would redo work it had already finished; step 8 refuses to
build a review unless steps 0-7 are promoted, so the manifest would have
blocked a run whose artifacts were all present; and specification 9.3 requires
a checkpoint after each step, which nothing was writing.

Ordering rule, from specification 9.3: checkpoint only after the artifacts the
manifest points at are durable. Every function here is therefore called after
the write, never before, and the manifest is saved atomically by the store.

Advancing runs through the intermediate statuses rather than jumping is
deliberate. ``advance_step`` rejects a jump, and that rejection is the check
that a driver did not skip verification.
"""

from datetime import datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import RunStatus, StepId, StepStatus
from glite_english_audit.artifacts.manifest import RunManifest
from glite_english_audit.state.machine import SEMANTIC_STEPS, advance_run, advance_step
from glite_english_audit.state.run_store import load_manifest, write_checkpoint

# The path a deterministic step walks from pending to promoted. A semantic
# step inserts VERIFIED_SEMANTIC before promotion, which the machine enforces.
_DETERMINISTIC_PATH: tuple[StepStatus, ...] = (
    StepStatus.IN_PROGRESS,
    StepStatus.PRODUCED,
    StepStatus.VERIFIED_DETERMINISTIC,
    StepStatus.PROMOTED,
)

_SEMANTIC_PATH: tuple[StepStatus, ...] = (
    StepStatus.IN_PROGRESS,
    StepStatus.PRODUCED,
    StepStatus.VERIFIED_DETERMINISTIC,
    StepStatus.VERIFIED_SEMANTIC,
    StepStatus.PROMOTED,
)


def _path_for(step: StepId) -> tuple[StepStatus, ...]:
    return _SEMANTIC_PATH if step in SEMANTIC_STEPS else _DETERMINISTIC_PATH


def _begin_processing(manifest: RunManifest) -> None:
    """Move the run into processing when a step starts doing work.

    A run sitting at ``awaiting_preflight`` or ``checkpointed`` is between
    steps; the first step transition of a driver is what makes it running
    again. A run already processing stays as it is.
    """
    if manifest.status in (RunStatus.AWAITING_PREFLIGHT, RunStatus.CHECKPOINTED):
        manifest.status = advance_run(manifest.status, RunStatus.PROCESSING)


def advance_to(
    run_id: str,
    step: StepId,
    target: StepStatus,
    *,
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
    producer_version: str | None = None,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Walk ``step`` forward to ``target`` and checkpoint the manifest.

    Intermediate statuses are stepped through in order, so a caller asking for
    ``PROMOTED`` from ``PENDING`` performs the whole legal sequence rather than
    an illegal jump. A step already at ``target`` is left alone, which makes
    this safe to call again after an interrupted run resumes.

    ``artifact_id``, ``artifact_hash``, and ``producer_version`` describe the
    artifact the step now points at. They are recorded once the step reaches
    ``PRODUCED``, because before that there is nothing to point at.
    """
    manifest = load_manifest(run_id, root=runs_root)
    state = manifest.steps[step]
    path = _path_for(step)

    if state.status is target:
        return manifest
    if target not in path:
        # Failure and quarantine are recorded by `mark_failed`, which does not
        # walk a path: they are reachable from wherever the step stands.
        msg = f"{target.value!r} is not on the promotion path for step {int(step)}"
        raise ValueError(msg)

    _begin_processing(manifest)

    remaining = path[path.index(state.status) + 1 :] if state.status in path else path
    for status in remaining:
        state.status = advance_step(state.status, status, step=step)
        if status is StepStatus.PRODUCED:
            state.current_artifact_id = artifact_id
            state.current_artifact_hash = artifact_hash
            state.producer_version = producer_version
        if status is target:
            break

    state.updated_at = now
    manifest.steps[step] = state
    return write_checkpoint(manifest, root=runs_root, now=now)


def require_promoted_through(
    run_id: str,
    last: StepId,
    *,
    runs_root: Path | None = None,
) -> None:
    """Refuse to continue unless every step up to ``last`` is promoted.

    The counts a review shows are the honesty guarantee of the whole audit:
    the denominator of analyzed words, the verified total, and the withheld
    classes that must add up to it. Computed from a partial run they are still
    arithmetically consistent and still wrong, and nothing downstream can tell
    the difference. The orchestration skill tells the agent to check this; the
    check belongs here as well, because a rule only an agent enforces is a rule
    that holds until an agent skips a step.
    """
    manifest = load_manifest(run_id, root=runs_root)
    unfinished = [
        step
        for step in StepId
        if int(step) <= int(last) and manifest.steps[step].status is not StepStatus.PROMOTED
    ]
    if unfinished:
        names = ", ".join(str(int(step)) for step in unfinished)
        # The list length decides the noun and the verb. "step 2, 3, 4 is not
        # promoted" was a plural subject with a singular verb.
        subject = f"step {names} is" if len(unfinished) == 1 else f"steps {names} are"
        msg = (
            f"this run cannot build a review yet: {subject} not promoted. "
            "The review's counts would describe part of the audit while claiming to "
            "describe all of it."
        )
        raise ValueError(msg)


def enter_review(
    run_id: str,
    *,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Move the run to ``review``: every local step is done, a person is not.

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
    step: StepId,
    *,
    quarantined: bool = False,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Record that a step failed, so resume repairs it instead of trusting it.

    A step that raised and left its status at ``in_progress`` is
    indistinguishable from one still running. Naming the failure is what lets
    the resume logic offer a repair rather than a redo.
    """
    manifest = load_manifest(run_id, root=runs_root)
    state = manifest.steps[step]
    target = StepStatus.QUARANTINED if quarantined else StepStatus.FAILED
    if state.status is StepStatus.PENDING:
        state.status = advance_step(state.status, StepStatus.IN_PROGRESS, step=step)
    if state.status is not target:
        state.status = advance_step(state.status, target, step=step)
    state.updated_at = now
    manifest.steps[step] = state
    return write_checkpoint(manifest, root=runs_root, now=now)


def output_is_current(run_id: str, step: StepId, *, runs_root: Path | None = None) -> bool:
    """Whether a file already on disk for ``step`` may be reused on a resume.

    False once the step is invalidated. ``invalidate_from`` only edits the
    manifest — it says so — and nothing deleted the files, so a resume after a
    changed skill, prompt or model saw them still sitting there, reported them
    as already written, and skipped exactly the judgments the change existed to
    replace. The run then reported the step recomputed.

    Reading the status rather than deleting the files keeps the old output
    inspectable: a person comparing what the new instructions did differently
    still has the previous answer next to it, in the quarantine directory or in
    the file the agent is about to overwrite.
    """
    state = load_manifest(run_id, root=runs_root).steps.get(step)
    if state is None:
        return False
    return state.status is not StepStatus.INVALIDATED
