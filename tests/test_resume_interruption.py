"""Interruption and resumption across the waterfall (specification, 13.5).

Every test here interrupts a real run and continues it from the run directory
alone, because that is the only thing a resumed audit is allowed to depend on:
the user continues in a fresh agent conversation, so nothing may be carried in
conversation history or in process memory (specification, 9.4). One test
therefore resumes in a genuinely fresh interpreter, as
``tests/test_pipeline_cli_subprocess.py`` does.

Stages 0 to 3, 7, and 8 run through their real drivers. Stages 4, 5, and 6 have
no driver: they run as skills, so their artifacts are simulated
deterministically here, the way ``tests/test_pipeline_waterfall.py`` does it.
The simulation is faithful where resumption depends on it — stage 4 writes one
findings file per utterance and marks a unit complete only after its body is
durable, which is what makes the utterance the smallest checkpoint unit
(specification, 9.3).

The run directory sits under the checkout's ``runtime/`` tree, so snapshots
land inside the run directory exactly as they do in production and retention
is tested against the real layout.
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.adapters.claude_code.adapter import ClaudeCodeAdapter
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    OsEnvironment,
    RunStatus,
    StageId,
    StageStatus,
)
from glite_english_audit.artifacts.envelope import ArtifactEnvelope
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, RunManifest
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    FindingsArtifactMeta,
    NormalizedUtterance,
    PrivateMistake,
    PrivateMistakesManifest,
    SafeMistakeRecord,
    SafeRecordCandidate,
    SourceInstanceRecord,
)
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline import (
    apply_authorship,
    authorship_batches,
    batches,
    build_review,
    collect,
    promote_records,
    start_run,
)
from glite_english_audit.state.event_log import log_event, read_events
from glite_english_audit.state.machine import SEMANTIC_STAGES, advance_run, advance_stage
from glite_english_audit.state.run_store import (
    RUN_MANIFEST_FILENAME,
    ResumeAssessment,
    ResumeDecision,
    RunStoreError,
    cleanup_completed,
    describe_resume,
    expire_stale_runs,
    invalidate_from,
    list_unfinished,
    load_manifest,
    next_incomplete_stage,
    save_manifest,
    write_checkpoint,
)
from glite_english_audit.verification.findings_format import (
    EMPTY_RESULT_LINE,
    THRESHOLD_LINE,
    TITLE_LINE,
    verify_findings_artifact,
)

_REPO = Path(__file__).resolve().parent.parent
_FIXTURE_HOME = _REPO / "fixtures" / "claude_code" / "success" / "home"

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=3)
_INVENTORY_NAME = "source-inventory.json"

# One deterministic judgment per marked construction, standing in for the model
# (specification, 7.1). The phrases are the synthetic fixture's own.
_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        "I very like this plan",
        "I really like this plan",
        "'Very' cannot modify a verb directly; use 'really'.",
    ),
    (
        "Today I written",
        "Today I wrote",
        "The simple past of 'write' is 'wrote', not the participle.",
    ),
    (
        "I am agree",
        "I agree",
        "'Agree' is a verb, so it takes no form of 'be'.",
    ),
)


@dataclass(frozen=True)
class Workspace:
    """A checkout, a source home, and the run store inside the checkout."""

    repo: Path
    home: Path
    inventory_dir: Path
    runs_root: Path


@dataclass(frozen=True)
class Resumed:
    """What one resume attempt decided and what it recomputed."""

    assessment: ResumeAssessment
    stages_run: tuple[StageId, ...]
    manifest: RunManifest


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    home = tmp_path / "home"
    shutil.copytree(_FIXTURE_HOME, home)
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    inventory_dir = ensure_private_dir(tmp_path / "inventory")
    _write_inventory(home, inventory_dir)
    return Workspace(
        repo=repo,
        home=home,
        inventory_dir=inventory_dir,
        runs_root=repo / "runtime" / "runs",
    )


def _write_inventory(home: Path, target: Path) -> None:
    outcome = claude_code_adapter().discover(
        DiscoveryContext(os_environment=OsEnvironment.MACOS, home=home, now=_NOW, environ={})
    )
    ensure_private_dir(target)
    write_model(
        target / _INVENTORY_NAME,
        PrivateInventory(
            records=outcome.records,
            instance_paths={key: str(path) for key, path in outcome.instance_paths.items()},
        ),
    )


def _start(workspace: Workspace, *, now: datetime = _NOW) -> RunManifest:
    return start_run.start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=workspace.runs_root,
        inventory_dir=workspace.inventory_dir,
        local_scan_consent=True,
        now=now,
    )


def _run_directory(workspace: Workspace, run_id: str) -> Path:
    return workspace.runs_root / run_id


def _corpus(workspace: Workspace, run_id: str) -> list[NormalizedUtterance]:
    path = stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=workspace.runs_root) / "corpus.jsonl"
    return list(read_jsonl_models(path, NormalizedUtterance))


def _findings_dir(workspace: Workspace, run_id: str) -> Path:
    return stage_dir(run_id, StageId.PLAIN_FINDINGS, root=workspace.runs_root) / "findings"


# -- stage production ------------------------------------------------------


def _write_authorship_decisions(
    workspace: Workspace,
    run_id: str,
    *,
    batch_limit: int | None = None,
    corrupt_marker: str | None = None,
) -> None:
    """Write one decisions file per candidate batch, standing in for the model.

    The judgment is fixed: material about the deploy script is treated as
    pasted, everything else is the learner's own. ``corrupt_marker`` names a
    candidate whose decision claims a span that is not in the text, which is
    what the deterministic checker must quarantine.
    """
    candidate_dir = authorship_batches.batch_dir(run_id, runs_root=workspace.runs_root)
    target = authorship_batches.decisions_dir(run_id, runs_root=workspace.runs_root)
    paths = sorted(candidate_dir.glob("batch-*.jsonl"))[:batch_limit]
    for path in paths:
        decisions: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            candidate = json.loads(line)
            text = str(candidate["text"])
            if corrupt_marker is not None and corrupt_marker in text:
                decisions.append(
                    {
                        "utterance_id": candidate["utterance_id"],
                        "decision": "partial",
                        "retained_spans": ["a span the learner never wrote"],
                        "reason": "AUTHORSHIP_AGENT_MACHINERY",
                    }
                )
            elif "deploy script" in text:
                decisions.append(
                    {
                        "utterance_id": candidate["utterance_id"],
                        "decision": "exclude",
                        "retained_spans": [],
                        "reason": "AUTHORSHIP_PASTED_MATERIAL",
                    }
                )
            else:
                decisions.append(
                    {
                        "utterance_id": candidate["utterance_id"],
                        "decision": "retain",
                        "retained_spans": [text],
                        "reason": None,
                    }
                )
        (target / path.name.replace("batch-", "decisions-")).write_text(
            "\n".join(json.dumps(decision, ensure_ascii=False) for decision in decisions) + "\n",
            encoding="utf-8",
        )


def _findings_body(text: str) -> tuple[str, int]:
    """The stage-4 body for one utterance, in the deterministic format."""
    blocks: list[list[str]] = []
    for original, correction, why in _MARKERS:
        if original in text:
            blocks.append(
                [
                    f"## Finding {len(blocks) + 1}",
                    "",
                    f"Original: {original}",
                    f"Correction: {correction}",
                    f"Why: {why}",
                ]
            )
    lines = [TITLE_LINE, "", THRESHOLD_LINE, ""]
    if not blocks:
        lines.append(EMPTY_RESULT_LINE)
        return "\n".join(lines) + "\n", 0
    for index, block in enumerate(blocks):
        if index:
            lines.append("")
        lines.extend(block)
    return "\n".join(lines) + "\n", len(blocks)


def _write_findings(
    workspace: Workspace,
    run_id: str,
    *,
    produced_at: datetime,
    unit_limit: int | None = None,
) -> list[str]:
    """Produce stage-4 findings, one file per utterance. Returns what it wrote.

    A unit already carrying a sidecar is skipped: the sidecar is written last,
    so its presence means that unit's body was durable before the process
    stopped (specification, 9.3).
    """
    target = ensure_private_dir(_findings_dir(workspace, run_id))
    produced: list[str] = []
    for utterance in _corpus(workspace, run_id):
        if unit_limit is not None and len(produced) >= unit_limit:
            break
        meta_path = target / f"{utterance.utterance_id}.meta.json"
        if meta_path.is_file():
            continue
        body, count = _findings_body(utterance.text)
        body_path = target / f"{utterance.utterance_id}.md"
        body_path.write_text(body, encoding="utf-8")
        write_model(
            meta_path,
            FindingsArtifactMeta(
                envelope=ArtifactEnvelope(
                    schema_name="plain_findings",
                    schema_version=1,
                    artifact_id=new_artifact_id(),
                    run_id=run_id,
                    stage_id=StageId.PLAIN_FINDINGS,
                    producer_name="resume-test-stand-in",
                    producer_version=CLIENT_VERSION,
                    created_at=produced_at,
                ),
                unit_id=utterance.utterance_id,
                utterance_ids=[utterance.utterance_id],
                finding_count=count,
                no_mistakes_found=count == 0,
                body_relative_path=body_path.name,
                body_sha256=sha256_hex(body_path.read_bytes()),
            ),
        )
        produced.append(utterance.utterance_id)
    return produced


def _write_private_mistakes(workspace: Workspace, run_id: str, *, at: datetime) -> None:
    """Turn stage-4 findings into stage-5 records, one per marked construction."""
    findings = _findings_dir(workspace, run_id)
    target = ensure_private_dir(
        stage_dir(run_id, StageId.PRIVATE_MISTAKES, root=workspace.runs_root)
    )
    mistakes: list[PrivateMistake] = []
    for utterance in _corpus(workspace, run_id):
        meta_path = findings / f"{utterance.utterance_id}.meta.json"
        if not meta_path.is_file():
            continue
        meta = read_model(meta_path, FindingsArtifactMeta)
        for original, correction, why in _MARKERS:
            start = utterance.text.find(original)
            if start < 0:
                continue
            index = len(mistakes) + 1
            mistakes.append(
                PrivateMistake(
                    mistake_id=f"m-{index}",
                    occurrence_id=f"m-{index}-o1",
                    finding_artifact_id=meta.envelope.artifact_id,
                    utterance_id=utterance.utterance_id,
                    evidence_span=EvidenceSpan(start=start, end=start + len(original)),
                    original_text=original,
                    correction=correction,
                    explanation=why,
                    modality=utterance.modality,
                    source_adapter=utterance.source_adapter,
                    session_hash=utterance.session_hash,
                )
            )
    path = target / "mistakes.jsonl"
    write_jsonl_models(path, mistakes)
    write_model(
        target / "private-mistakes-manifest.json",
        PrivateMistakesManifest(
            envelope=ArtifactEnvelope(
                schema_name="private_mistakes",
                schema_version=1,
                artifact_id=new_artifact_id(),
                run_id=run_id,
                stage_id=StageId.PRIVATE_MISTAKES,
                producer_name="resume-test-stand-in",
                producer_version=CLIENT_VERSION,
                created_at=at,
            ),
            mistake_count=len(mistakes),
            jsonl_relative_path=path.name,
            jsonl_sha256=sha256_hex(path.read_bytes()),
        ),
    )


def _write_safe_candidates(workspace: Workspace, run_id: str) -> None:
    """Stage 6: one candidate per mistake, one of them deliberately unsafe.

    The unsafe one carries an address, so the deterministic scanner withholds
    it and the withheld count has something real to preserve across a resume.
    """
    mistakes = list(
        read_jsonl_models(
            stage_dir(run_id, StageId.PRIVATE_MISTAKES, root=workspace.runs_root)
            / "mistakes.jsonl",
            PrivateMistake,
        )
    )
    target = ensure_private_dir(stage_dir(run_id, StageId.SAFE_RECORDS, root=workspace.runs_root))
    candidates: list[SafeRecordCandidate] = []
    for position, mistake in enumerate(mistakes):
        unsafe = position == 1
        example = (
            "Mail the summary to someone@example.com first."
            if unsafe
            else f"{mistake.correction} today."
        )
        candidates.append(
            SafeRecordCandidate(
                mistake_id=mistake.mistake_id,
                record=SafeMistakeRecord(
                    mistake=f"Wrote '{mistake.original_text}'.",
                    rule=mistake.explanation,
                    example=example,
                    example_type=ExampleType.SYNTHETIC,
                    source_type=mistake.source_adapter,
                    modality=mistake.modality,
                ),
                creator_version=CLIENT_VERSION,
            )
        )
    write_jsonl_models(target / "candidates.jsonl", candidates)


def _produce(
    workspace: Workspace,
    run_id: str,
    stage: StageId,
    *,
    at: datetime,
    unit_limit: int | None = None,
) -> None:
    """Produce exactly one stage's artifacts.

    Stages 1 and 2 share one driver: ``collect`` snapshots and extracts in a
    single call, so the snapshot stage produces both and the candidate stage
    has nothing left to do.
    """
    if stage is StageId.SOURCE_INVENTORY:
        _write_inventory(
            workspace.home, stage_dir(run_id, StageId.SOURCE_INVENTORY, root=workspace.runs_root)
        )
    elif stage is StageId.SOURCE_SNAPSHOTS:
        collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    elif stage is StageId.CANDIDATE_UTTERANCES:
        pass
    elif stage is StageId.ELIGIBLE_ENGLISH:
        authorship_batches.prepare_authorship_batches(
            run_id, batch_size=5, runs_root=workspace.runs_root
        )
        _write_authorship_decisions(workspace, run_id)
        apply_authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
    elif stage is StageId.PLAIN_FINDINGS:
        batches.prepare_batches(run_id, batch_size=5, runs_root=workspace.runs_root)
        _write_findings(workspace, run_id, produced_at=at, unit_limit=unit_limit)
    elif stage is StageId.PRIVATE_MISTAKES:
        _write_private_mistakes(workspace, run_id, at=at)
    elif stage is StageId.SAFE_RECORDS:
        _write_safe_candidates(workspace, run_id)
    elif stage is StageId.PRIVACY_APPROVED:
        promote_records.promote(run_id, runs_root=workspace.runs_root)
    elif stage is StageId.REVIEWED_SUBMISSION:
        build_review.build_review(run_id, runs_root=workspace.runs_root)


# -- manifest bookkeeping the orchestration skill performs -----------------


def _is_content_file(path: Path) -> bool:
    """True for files two identical runs must reproduce byte for byte.

    Manifests and sidecars carry a fresh artifact ID and creation time by
    design, so they differ between two identical passes. They are compared only
    where the point is that a stage was *not* rerun.
    """
    return path.suffix in {".jsonl", ".md"} or path.name in {"withheld.json", _INVENTORY_NAME}


def _stage_files(
    workspace: Workspace, run_id: str, stage: StageId, *, content_only: bool
) -> dict[str, bytes]:
    root = stage_dir(run_id, stage, root=workspace.runs_root)
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and (not content_only or _is_content_file(path))
    }


def _stage_hash(workspace: Workspace, run_id: str, stage: StageId) -> str:
    files = _stage_files(workspace, run_id, stage, content_only=True)
    blob = b"".join(name.encode("utf-8") + b"\0" + data for name, data in sorted(files.items()))
    return sha256_hex(blob)


def _promote_stage(
    workspace: Workspace, manifest: RunManifest, stage: StageId, *, at: datetime
) -> None:
    """Walk one stage to PROMOTED through the real transition table.

    A stage left in progress by an interruption is continued, not re-entered:
    the table has no self-transition, and a resumed process is the same work
    picked up again.
    """
    state = manifest.stages[stage]
    targets = [StageStatus.PRODUCED, StageStatus.VERIFIED_DETERMINISTIC]
    if state.status is not StageStatus.IN_PROGRESS:
        targets.insert(0, StageStatus.IN_PROGRESS)
    if stage in SEMANTIC_STAGES:
        targets.append(StageStatus.VERIFIED_SEMANTIC)
    targets.append(StageStatus.PROMOTED)
    for target in targets:
        state.status = advance_stage(state.status, target, stage=stage)
    state.current_artifact_id = new_artifact_id()
    state.current_artifact_hash = _stage_hash(workspace, manifest.run_id, stage)
    state.updated_at = at


def _enter_processing(manifest: RunManifest, *, refreshed_preflight: bool = False) -> None:
    if manifest.status is RunStatus.PROCESSING and not refreshed_preflight:
        return
    if manifest.status is RunStatus.PROCESSING:
        manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    if refreshed_preflight and manifest.status is not RunStatus.AWAITING_PREFLIGHT:
        manifest.status = advance_run(manifest.status, RunStatus.AWAITING_PREFLIGHT)
    manifest.status = advance_run(manifest.status, RunStatus.PROCESSING)


def _advance_through(
    workspace: Workspace, manifest: RunManifest, last: StageId, *, at: datetime
) -> None:
    """Run, promote, and checkpoint every stage up to and including ``last``."""
    _enter_processing(manifest)
    for stage in StageId:
        if stage > last:
            break
        if manifest.stages[stage].status is StageStatus.PROMOTED:
            continue
        _produce(workspace, manifest.run_id, stage, at=at)
        _promote_stage(workspace, manifest, stage, at=at)
        write_checkpoint(manifest, root=workspace.runs_root, now=at)


def _resume(
    workspace: Workspace,
    run_id: str,
    current: CompatibilityFingerprint,
    *,
    at: datetime = _LATER,
) -> Resumed:
    """Continue a run from the manifest on disk, as a fresh conversation must.

    Nothing from the interrupted pass is passed in: the manifest is reread, the
    policy is applied to it, and work restarts at the stage the policy names.
    """
    manifest = load_manifest(run_id, root=workspace.runs_root)
    assessment = describe_resume(manifest, current, now=at)
    if assessment.decision in (ResumeDecision.RESTART, ResumeDecision.EXPIRED):
        return Resumed(assessment=assessment, stages_run=(), manifest=manifest)
    if assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM:
        assert assessment.earliest_affected_stage is not None
        invalidate_from(manifest, assessment.earliest_affected_stage, now=at)
        manifest.fingerprint = current
        _enter_processing(manifest, refreshed_preflight=True)
    else:
        _enter_processing(manifest)
    save_manifest(manifest, root=workspace.runs_root)

    started = next_incomplete_stage(manifest)
    ran: list[StageId] = []
    if started is not None:
        for stage in StageId:
            if stage < started:
                continue
            _produce(workspace, run_id, stage, at=at)
            _promote_stage(workspace, manifest, stage, at=at)
            write_checkpoint(manifest, root=workspace.runs_root, now=at)
            ran.append(stage)
    return Resumed(assessment=assessment, stages_run=tuple(ran), manifest=manifest)


def _changed(
    fingerprint: CompatibilityFingerprint, **overrides: object
) -> CompatibilityFingerprint:
    return fingerprint.model_copy(update=overrides)


# -- the run never completed ------------------------------------------------


def test_run_interrupted_before_its_manifest_offers_nothing_to_resume(
    workspace: Workspace,
) -> None:
    # start_run writes the stage-0 inventory before the manifest, so a crash
    # between them leaves a directory with private data and no state file. It
    # must not be offered for resume, and it must not break the listing.
    manifest = _start(workspace)
    (_run_directory(workspace, manifest.run_id) / RUN_MANIFEST_FILENAME).unlink()

    assert list_unfinished(workspace.runs_root, now=_NOW) == []


# -- interrupt before the checkpoint ---------------------------------------


@pytest.mark.parametrize("stage", list(StageId), ids=lambda stage: f"stage{int(stage)}")
def test_interrupt_before_checkpoint_reruns_the_stage(workspace: Workspace, stage: StageId) -> None:
    """The artifacts are durable but the checkpoint never landed.

    The two tests below cover the other shape of the same interruption, where
    the stage stopped part way through its own work.
    """
    manifest = _start(workspace)
    run_id = manifest.run_id
    previous = StageId(stage - 1) if stage > StageId.SOURCE_INVENTORY else None
    if previous is not None:
        _advance_through(workspace, manifest, previous, at=_NOW)
    else:
        _enter_processing(manifest)
        write_checkpoint(manifest, root=workspace.runs_root, now=_NOW)

    upstream_before = {
        earlier: _stage_files(workspace, run_id, earlier, content_only=False)
        for earlier in StageId
        if previous is not None and earlier <= previous
    }
    # The interrupted stage: produced, marked in progress, never checkpointed.
    _produce(workspace, run_id, stage, at=_NOW)
    manifest.stages[stage].status = advance_stage(
        manifest.stages[stage].status, StageStatus.IN_PROGRESS, stage=stage
    )
    save_manifest(manifest, root=workspace.runs_root)
    interrupted_content = _stage_files(workspace, run_id, stage, content_only=True)

    reread = load_manifest(run_id, root=workspace.runs_root)
    assert reread.last_checkpoint_at == _NOW
    assert reread.stages[stage].status is StageStatus.IN_PROGRESS
    assert reread.stages[stage].current_artifact_id is None
    assert next_incomplete_stage(reread) is stage

    resumed = _resume(workspace, run_id, reread.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.stages_run[0] is stage
    assert resumed.stages_run[-1] is StageId.REVIEWED_SUBMISSION
    # The rerun reproduces the interrupted stage exactly, and nothing earlier
    # was touched: the interruption cost work, never data.
    assert _stage_files(workspace, run_id, stage, content_only=True) == interrupted_content
    for earlier, before in upstream_before.items():
        assert _stage_files(workspace, run_id, earlier, content_only=False) == before
    final = load_manifest(run_id, root=workspace.runs_root)
    assert final.stages[stage].status is StageStatus.PROMOTED
    assert final.last_checkpoint_at == _LATER


def test_interrupt_inside_stage_three_reruns_only_what_was_lost(workspace: Workspace) -> None:
    # The authorship judgment stopped after the first of two batches, before
    # anything applied the decisions. Nothing is promoted, so the whole stage
    # reruns and the corpus is complete.
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.CANDIDATE_UTTERANCES, at=_NOW)

    index = authorship_batches.prepare_authorship_batches(
        run_id, batch_size=5, runs_root=workspace.runs_root
    )
    assert len(index.batches) == 2
    _write_authorship_decisions(workspace, run_id, batch_limit=1)
    decisions = authorship_batches.decisions_dir(run_id, runs_root=workspace.runs_root)
    assert len(list(decisions.glob("decisions-*.jsonl"))) == 1

    resumed = _resume(workspace, run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.stages_run[0] is StageId.ELIGIBLE_ENGLISH
    assert len(list(decisions.glob("decisions-*.jsonl"))) == 2
    corpus = _corpus(workspace, run_id)
    assert len(corpus) == index.candidate_count - 1  # the pasted candidate is excluded
    assert all(u.text for u in corpus)


def test_interrupted_unit_is_rerun_and_promoted_units_are_not(workspace: Workspace) -> None:
    """The utterance is the checkpoint unit, not the batch (specification, 9.3)."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)
    unit_count = len(_corpus(workspace, run_id))
    assert unit_count > 4

    first_pass = _write_findings(workspace, run_id, produced_at=_NOW, unit_limit=4)
    assert len(first_pass) == 4
    # A unit whose body was written but whose sidecar never landed is not
    # promoted, so the resume has to redo it.
    (_findings_dir(workspace, run_id) / f"{first_pass[-1]}.meta.json").unlink()
    promoted = first_pass[:-1]
    before = {
        name: data
        for name, data in _stage_files(
            workspace, run_id, StageId.PLAIN_FINDINGS, content_only=False
        ).items()
        if any(unit in name for unit in promoted)
    }

    second_pass = _write_findings(workspace, run_id, produced_at=_LATER)

    assert first_pass[-1] in second_pass
    assert not set(promoted) & set(second_pass)
    assert len(promoted) + len(second_pass) == unit_count
    after = _stage_files(workspace, run_id, StageId.PLAIN_FINDINGS, content_only=False)
    for name, data in before.items():
        assert after[name] == data, f"a promoted unit was rewritten: {name}"
    for unit in promoted:
        meta = read_model(
            _findings_dir(workspace, run_id) / f"{unit}.meta.json", FindingsArtifactMeta
        )
        assert meta.envelope.created_at == _NOW
    for unit in second_pass:
        directory = _findings_dir(workspace, run_id)
        meta = read_model(directory / f"{unit}.meta.json", FindingsArtifactMeta)
        assert meta.envelope.created_at == _LATER
        assert verify_findings_artifact(directory / f"{unit}.md", meta, item_ref=unit) == []


# -- interrupt after the checkpoint ----------------------------------------


@pytest.mark.parametrize("stage", list(StageId), ids=lambda stage: f"stage{int(stage)}")
def test_interrupt_after_checkpoint_does_not_reprocess(
    workspace: Workspace, stage: StageId
) -> None:
    """Everything through ``stage`` is promoted and checkpointed."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, stage, at=_NOW)
    # A clean stop: the run checkpointed and the process ended.
    manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    save_manifest(manifest, root=workspace.runs_root)

    done = [earlier for earlier in StageId if earlier <= stage]
    before = {
        earlier: _stage_files(workspace, run_id, earlier, content_only=False) for earlier in done
    }
    identifiers = {
        earlier: manifest.stages[earlier].current_artifact_id
        for earlier in done
        if manifest.stages[earlier].current_artifact_id is not None
    }

    resumed = _resume(workspace, run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert set(resumed.stages_run).isdisjoint(done)
    for earlier in done:
        assert _stage_files(workspace, run_id, earlier, content_only=False) == before[earlier]
    final = load_manifest(run_id, root=workspace.runs_root)
    for earlier, artifact_id in identifiers.items():
        assert final.stages[earlier].current_artifact_id == artifact_id
        assert final.stages[earlier].current_artifact_hash == _stage_hash(
            workspace, run_id, earlier
        )
    assert next_incomplete_stage(final) is None
    assert final.status is RunStatus.PROCESSING


# -- resumption in a fresh process -----------------------------------------


_RESUME_PROBE = """
import json
import sys
from datetime import datetime
from pathlib import Path

from glite_english_audit.state.run_store import (
    describe_resume,
    list_unfinished,
    load_manifest,
    next_incomplete_stage,
)

runs_root = Path(sys.argv[1])
run_id = sys.argv[2]
moment = datetime.fromisoformat(sys.argv[3])
manifest = load_manifest(run_id, root=runs_root)
assessment = describe_resume(manifest, manifest.fingerprint, now=moment)
stage = next_incomplete_stage(manifest)
print(
    json.dumps(
        {
            "decision": assessment.decision.value,
            "detail": assessment.detail,
            "next_stage": None if stage is None else int(stage),
            "unfinished": [
                summary.run_id for summary in list_unfinished(runs_root, now=moment)
            ],
        }
    )
)
"""


def _fresh_process(script: Path, *args: str, cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return dict(json.loads(result.stdout))


def _driver_process(module: str, *args: str, cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{module} failed: {result.stderr[-2000:]}"
    return dict(json.loads(result.stdout))


def test_resume_works_in_a_fresh_process(workspace: Workspace, tmp_path: Path) -> None:
    """Resumption may not depend on the conversation or on process state."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.SAFE_RECORDS, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.CHECKPOINTED)
    save_manifest(manifest, root=workspace.runs_root)

    probe = tmp_path / "resume_probe.py"
    probe.write_text(_RESUME_PROBE, encoding="utf-8")
    # cwd is deliberately outside the checkout: a resumed run reads the run
    # store, never the working directory it was started from. The clock is
    # passed in, so the assertions below do not age.
    payload = _fresh_process(
        probe, str(workspace.runs_root), run_id, _LATER.isoformat(), cwd=tmp_path
    )

    assert payload["decision"] == ResumeDecision.CONTINUE.value
    assert payload["next_stage"] == int(StageId.PRIVACY_APPROVED)
    assert payload["unfinished"] == [run_id]

    promoted = _driver_process(
        "glite_english_audit.pipeline.promote_records",
        "--run-id",
        run_id,
        "--runs-root",
        str(workspace.runs_root),
        cwd=tmp_path,
    )
    reviewed = _driver_process(
        "glite_english_audit.pipeline.build_review",
        "--run-id",
        run_id,
        "--runs-root",
        str(workspace.runs_root),
        cwd=tmp_path,
    )

    assert promoted["approved"] == 2
    assert promoted["withheld_for_privacy"] == 1
    assert reviewed["records"] == 2
    assert reviewed["verified_total_mistakes"] == 3
    # Driver output reaches the agent conversation, so it carries counts only.
    blob = json.dumps([promoted, reviewed, payload])
    for phrase in ("I very like", "I am agree", "someone@example.com"):
        assert phrase not in blob


# -- the frozen record cutoff ----------------------------------------------


def test_record_after_the_cutoff_belongs_to_the_next_audit(workspace: Workspace) -> None:
    """New source records never invalidate a resume (specification, 13.5)."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.CANDIDATE_UTTERANCES, at=_NOW)
    selection = manifest.selection
    assert selection is not None
    assert selection.record_cutoff_at == _NOW
    candidates = (
        stage_dir(run_id, StageId.CANDIDATE_UTTERANCES, root=workspace.runs_root)
        / "candidates.jsonl"
    )
    before = candidates.read_bytes()

    transcript = (
        workspace.home
        / ".claude/projects/-home-tester-notes/22222222-2222-4222-8222-222222222222.jsonl"
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "uuid": "later-1",
                    "parentUuid": None,
                    "timestamp": (_NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    "sessionId": "22222222-2222-4222-8222-222222222222",
                    "cwd": "/home/tester/notes",
                    "version": "2.1.210",
                    "gitBranch": "main",
                    "userType": "external",
                    "entrypoint": "cli",
                    "isSidechain": False,
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Tomorrow I will writing one more note.",
                    },
                }
            )
            + "\n"
        )

    resumed = _resume(workspace, run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert candidates.read_bytes() == before

    # The record is real and in period: a later audit, with a later cutoff,
    # picks it up. That is what makes the exclusion above a cutoff decision.
    next_audit = _start(workspace, now=_NOW + timedelta(days=2))
    _write_inventory(
        workspace.home,
        stage_dir(next_audit.run_id, StageId.SOURCE_INVENTORY, root=workspace.runs_root),
    )
    counts = collect.collect(next_audit.run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    assert int(str(counts["candidate_utterances"])) == len(before.decode("utf-8").splitlines()) + 1


# -- the four resume decisions ---------------------------------------------


def test_resume_continue_finishes_the_remaining_stages(workspace: Workspace) -> None:
    manifest = _start(workspace)
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)

    resumed = _resume(workspace, manifest.run_id, manifest.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.stages_run == (
        StageId.PLAIN_FINDINGS,
        StageId.PRIVATE_MISTAKES,
        StageId.SAFE_RECORDS,
        StageId.PRIVACY_APPROVED,
        StageId.REVIEWED_SUBMISSION,
    )
    reviewed = build_review.build_review(manifest.run_id, runs_root=workspace.runs_root)
    counts = reviewed.counts
    assert counts.shared_mistakes + counts.withheld_for_privacy == counts.verified_total_mistakes


def test_resume_invalidate_downstream_recomputes_from_the_semantic_stages(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.REVIEWED_SUBMISSION, at=_NOW)
    kept = {
        stage: _stage_files(workspace, run_id, stage, content_only=False)
        for stage in StageId
        if stage <= StageId.ELIGIBLE_ENGLISH
    }
    corpus_artifact = manifest.stages[StageId.ELIGIBLE_ENGLISH].current_artifact_id

    changed = _changed(manifest.fingerprint, skill_versions={"analyze-english-text": 2})
    resumed = _resume(workspace, run_id, changed)

    assert resumed.assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert resumed.assessment.earliest_affected_stage is StageId.PLAIN_FINDINGS
    assert resumed.stages_run == (
        StageId.PLAIN_FINDINGS,
        StageId.PRIVATE_MISTAKES,
        StageId.SAFE_RECORDS,
        StageId.PRIVACY_APPROVED,
        StageId.REVIEWED_SUBMISSION,
    )
    # Everything upstream of the change is kept, byte for byte and by pointer.
    for stage, before in kept.items():
        assert _stage_files(workspace, run_id, stage, content_only=False) == before
    final = load_manifest(run_id, root=workspace.runs_root)
    assert final.stages[StageId.ELIGIBLE_ENGLISH].current_artifact_id == corpus_artifact
    assert final.fingerprint == changed
    assert all(
        final.stages[stage].status is StageStatus.PROMOTED
        for stage in StageId
        if stage >= StageId.PLAIN_FINDINGS
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"skill_versions": {"filter-authored-english": 3}},
        {"prompt_versions": {"find-mistakes": 2}},
        {"model_ids": {"find-mistakes": "example-model-2"}},
    ],
    ids=["skill", "prompt", "model"],
)
def test_resume_detects_a_changed_skill_prompt_or_model(
    workspace: Workspace, overrides: dict[str, object]
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.PLAIN_FINDINGS, at=_NOW)
    kept = _stage_files(workspace, run_id, StageId.ELIGIBLE_ENGLISH, content_only=False)

    resumed = _resume(workspace, run_id, _changed(manifest.fingerprint, **overrides))

    assert resumed.assessment.decision is ResumeDecision.INVALIDATE_DOWNSTREAM
    assert resumed.assessment.earliest_affected_stage is StageId.PLAIN_FINDINGS
    assert resumed.stages_run[0] is StageId.PLAIN_FINDINGS
    assert _stage_files(workspace, run_id, StageId.ELIGIBLE_ENGLISH, content_only=False) == kept


@pytest.mark.parametrize(
    "overrides",
    [
        {"adapter_versions": {"claude_code": "99.0.0"}},
        {"artifact_schema_version": 2},
        {"tokenizer_version": "9.9.9"},
        {"consent_policy_version": "2"},
    ],
    ids=["adapter", "schema", "tokenizer", "consent"],
)
def test_resume_detects_an_incompatible_change(
    workspace: Workspace, overrides: dict[str, object]
) -> None:
    # No versioned deterministic artifact migration exists yet, so every one of
    # these changes takes the conservative branch: reuse nothing
    # (specification, 9.4).
    manifest = _start(workspace)
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)

    resumed = _resume(workspace, manifest.run_id, _changed(manifest.fingerprint, **overrides))

    assert resumed.assessment.decision is ResumeDecision.RESTART
    assert resumed.stages_run == ()


def test_invalidate_from_clears_pointers_and_keeps_a_quarantined_stage(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    _advance_through(workspace, manifest, StageId.PRIVATE_MISTAKES, at=_NOW)
    findings = manifest.stages[StageId.PLAIN_FINDINGS]
    findings.status = advance_stage(
        findings.status, StageStatus.IN_PROGRESS, stage=StageId.PLAIN_FINDINGS
    )
    findings.status = advance_stage(
        findings.status, StageStatus.QUARANTINED, stage=StageId.PLAIN_FINDINGS
    )

    invalidated = invalidate_from(manifest, StageId.PLAIN_FINDINGS, now=_LATER)

    # A quarantined stage keeps its status, so its diagnostic history is not
    # overwritten by the invalidation decision.
    assert invalidated == [StageId.PRIVATE_MISTAKES]
    assert manifest.stages[StageId.PLAIN_FINDINGS].status is StageStatus.QUARANTINED
    downstream = manifest.stages[StageId.PRIVATE_MISTAKES]
    assert downstream.status is StageStatus.INVALIDATED
    assert downstream.current_artifact_id is None
    assert downstream.current_artifact_hash is None
    assert manifest.stages[StageId.ELIGIBLE_ENGLISH].status is StageStatus.PROMOTED
    assert manifest.stages[StageId.ELIGIBLE_ENGLISH].current_artifact_id is not None


def test_resume_restart_reuses_nothing_and_leaves_the_old_run_alone(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)
    before = {
        stage: _stage_files(workspace, run_id, stage, content_only=False) for stage in StageId
    }

    resumed = _resume(workspace, run_id, _changed(manifest.fingerprint, tokenizer_version="9.9.9"))

    assert resumed.assessment.decision is ResumeDecision.RESTART
    assert resumed.stages_run == ()
    for stage in StageId:
        assert _stage_files(workspace, run_id, stage, content_only=False) == before[stage]
    unchanged = load_manifest(run_id, root=workspace.runs_root)
    assert unchanged.stages[StageId.ELIGIBLE_ENGLISH].status is StageStatus.PROMOTED

    # A restart is a new run: the old one stays listed until retention or the
    # user's own decision removes it.
    fresh = _start(workspace, now=_LATER)
    assert {summary.run_id for summary in list_unfinished(workspace.runs_root, now=_LATER)} == {
        run_id,
        fresh.run_id,
    }


def test_resume_expired_after_the_retention_limit(workspace: Workspace) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)
    expired_moment = _NOW + timedelta(days=30, seconds=1)

    resumed = _resume(workspace, run_id, manifest.fingerprint, at=expired_moment)

    assert resumed.assessment.decision is ResumeDecision.EXPIRED
    assert "new audit" in resumed.assessment.detail
    assert resumed.stages_run == ()

    assert expire_stale_runs(workspace.runs_root, now=expired_moment) == [run_id]
    run_directory = _run_directory(workspace, run_id)
    assert (run_directory / RUN_MANIFEST_FILENAME).is_file()
    for name in ("stages", "logs", "snapshots", "submission"):
        assert not (run_directory / name).exists()

    # The status alone is enough afterwards: a checkpoint written later must
    # not make a run whose private inputs were deleted look resumable.
    swept = load_manifest(run_id, root=workspace.runs_root)
    assert swept.status is RunStatus.EXPIRED
    write_checkpoint(swept, root=workspace.runs_root, now=expired_moment)
    assert (
        describe_resume(swept, swept.fingerprint, now=expired_moment).decision
        is ResumeDecision.EXPIRED
    )


def test_thirty_day_boundary_keeps_a_run_resumable(workspace: Workspace) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    _advance_through(workspace, manifest, StageId.ELIGIBLE_ENGLISH, at=_NOW)
    boundary = _NOW + timedelta(days=30)

    assert (
        describe_resume(manifest, manifest.fingerprint, now=boundary).decision
        is ResumeDecision.CONTINUE
    )
    assert expire_stale_runs(workspace.runs_root, now=boundary) == []
    assert stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=workspace.runs_root).is_dir()

    resumed = _resume(workspace, run_id, manifest.fingerprint, at=boundary)
    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.stages_run[0] is StageId.PLAIN_FINDINGS


def test_a_completed_run_is_never_offered_for_resume(workspace: Workspace) -> None:
    manifest = _start(workspace)
    _advance_through(workspace, manifest, StageId.REVIEWED_SUBMISSION, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)

    assessment = describe_resume(manifest, manifest.fingerprint, now=_LATER)

    assert assessment.decision is ResumeDecision.EXPIRED
    assert list_unfinished(workspace.runs_root, now=_LATER) == []


# -- quarantined failures --------------------------------------------------


def test_quarantined_failures_and_diagnostics_survive_a_resume(workspace: Workspace) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    run_directory = _run_directory(workspace, run_id)
    _advance_through(workspace, manifest, StageId.CANDIDATE_UTTERANCES, at=_NOW)

    authorship_batches.prepare_authorship_batches(
        run_id, batch_size=5, runs_root=workspace.runs_root
    )
    _write_authorship_decisions(workspace, run_id, corrupt_marker="I very like")
    applied = apply_authorship.apply_authorship(run_id, runs_root=workspace.runs_root)
    codes = sorted({diagnostic.code for diagnostic in applied.diagnostics})
    assert applied.quarantined_decisions == 1
    assert codes == ["AUTHORSHIP_SPAN_NOT_VERBATIM"]
    for diagnostic in applied.diagnostics:
        log_event(
            run_directory,
            "item_quarantined",
            stage_id=StageId.ELIGIBLE_ENGLISH,
            diagnostic_codes=[diagnostic.code],
        )
    state = manifest.stages[StageId.ELIGIBLE_ENGLISH]
    state.status = advance_stage(
        state.status, StageStatus.IN_PROGRESS, stage=StageId.ELIGIBLE_ENGLISH
    )
    state.status = advance_stage(
        state.status, StageStatus.QUARANTINED, stage=StageId.ELIGIBLE_ENGLISH
    )
    write_checkpoint(manifest, root=workspace.runs_root, now=_NOW)

    # A fresh read of the run: the quarantine and its history are still there.
    reread = load_manifest(run_id, root=workspace.runs_root)
    assert reread.stages[StageId.ELIGIBLE_ENGLISH].status is StageStatus.QUARANTINED
    assert next_incomplete_stage(reread) is StageId.ELIGIBLE_ENGLISH
    events = read_events(run_directory)
    assert [event.kind for event in events] == ["item_quarantined"]
    assert events[0].diagnostic_codes == ["AUTHORSHIP_SPAN_NOT_VERBATIM"]

    resumed = _resume(workspace, run_id, reread.fingerprint)

    assert resumed.assessment.decision is ResumeDecision.CONTINUE
    assert resumed.stages_run[0] is StageId.ELIGIBLE_ENGLISH
    # The resume re-asks for the quarantined unit and this time the judgment
    # verifies, so the utterance rejoins the corpus.
    assert any("I very like" in utterance.text for utterance in _corpus(workspace, run_id))
    # The repair does not erase what the failed pass recorded.
    assert [event.kind for event in read_events(run_directory)] == ["item_quarantined"]
    assert (
        load_manifest(run_id, root=workspace.runs_root).stages[StageId.ELIGIBLE_ENGLISH].status
        is StageStatus.PROMOTED
    )
    # The log is content-free by construction; prove it over the real bytes.
    log_bytes = (run_directory / "logs" / "events.jsonl").read_bytes()
    for phrase in (b"I very like", b"deploy script", b"learning progress"):
        assert phrase not in log_bytes


# -- retention -------------------------------------------------------------


def _failing_extract(
    self: ClaudeCodeAdapter, instance: SourceInstanceRecord, snapshot_dir: Path
) -> Iterator[NormalizedUtterance]:
    msg = "extraction failed after the snapshot was taken"
    raise RuntimeError(msg)


def test_expiry_deletes_snapshots_left_by_a_failed_extraction(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest-bounded snapshot cleanup never ran, so retention must reach them."""
    manifest = _start(workspace)
    run_id = manifest.run_id
    monkeypatch.setattr(ClaudeCodeAdapter, "extract", _failing_extract)
    result = collect.collect(run_id, runs_root=workspace.runs_root, repo=workspace.repo)
    assert result["excluded_instances"], "the failure must be reported, not hidden"

    snapshots = _run_directory(workspace, run_id) / "snapshots"
    copied = [path for path in snapshots.rglob("*") if path.is_file()]
    assert copied, "an interrupted extraction leaves the copied source behind"
    assert any(b"learning progress" in path.read_bytes() for path in copied)

    manifest.last_checkpoint_at = _NOW - timedelta(days=31)
    save_manifest(manifest, root=workspace.runs_root)
    assert expire_stale_runs(workspace.runs_root, now=_NOW) == [run_id]
    assert not snapshots.exists()


def test_completed_cleanup_keeps_only_the_package_and_the_manifest(
    workspace: Workspace,
) -> None:
    manifest = _start(workspace)
    run_id = manifest.run_id
    run_directory = _run_directory(workspace, run_id)
    _advance_through(workspace, manifest, StageId.REVIEWED_SUBMISSION, at=_NOW)
    log_event(run_directory, "checkpoint_written", stage_id=StageId.REVIEWED_SUBMISSION)
    (run_directory / "submission" / "package.json").write_text("{}", encoding="utf-8")
    # A source whose extraction failed can leave a snapshot behind; completion
    # must not keep it either.
    leftover = run_directory / "snapshots" / "claude_code" / "abc"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "sessions.jsonl").write_text("I very like this plan\n", encoding="utf-8")
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)

    cleanup_completed(run_directory)

    survivors = sorted(
        str(path.relative_to(run_directory)) for path in run_directory.rglob("*") if path.is_file()
    )
    assert survivors == [RUN_MANIFEST_FILENAME, "submission/package.json"]
    for path in run_directory.rglob("*"):
        if path.is_file():
            assert b"I very like" not in path.read_bytes()
    assert load_manifest(run_id, root=workspace.runs_root).status is RunStatus.COMPLETED


# -- cleanup can never leave the run directory -----------------------------


def _source_home(tmp_path: Path) -> Path:
    """A stand-in for the user's own application data and home directory."""
    home = tmp_path / "victim-home"
    projects = home / ".claude" / "projects" / "-home-tester-notes"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text("private source text\n", encoding="utf-8")
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.txt").write_text("keep", encoding="utf-8")
    return home


def _victim_files(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def test_cleanup_refuses_a_run_directory_that_is_a_symlink(
    workspace: Workspace, tmp_path: Path
) -> None:
    # The worst case for a store that deletes fixed names under a directory: a
    # link named like a run, holding a manifest that matches that name. Every
    # containment check resolves the run directory first, so without a check on
    # the link itself the target's own 'stages' and 'logs' are inside the
    # bounds and a home directory is one deletion away.
    home = _source_home(tmp_path)
    planted_id = "run-" + "a" * 32
    for name in ("stages", "logs", "snapshots", "submission"):
        (home / name).mkdir()
        (home / name / "keep.txt").write_text("keep", encoding="utf-8")
    real = _start(workspace)
    completed = load_manifest(real.run_id, root=workspace.runs_root).model_copy(
        update={"run_id": planted_id, "status": RunStatus.COMPLETED}
    )
    write_model(home / RUN_MANIFEST_FILENAME, completed)
    before = _victim_files(home)
    planted = workspace.runs_root / planted_id
    planted.symlink_to(home, target_is_directory=True)

    with pytest.raises(RunStoreError, match="symlink") as excinfo:
        cleanup_completed(planted)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_UNSAFE_CLEANUP_PATH"

    # The same link, now claiming an unfinished run far past the retention
    # limit, is what the launch-time sweep walks over.
    stale = completed.model_copy(
        update={"status": RunStatus.CHECKPOINTED, "last_checkpoint_at": _NOW - timedelta(days=400)}
    )
    write_model(home / RUN_MANIFEST_FILENAME, stale)
    assert expire_stale_runs(workspace.runs_root, now=_NOW) == []
    assert [summary.run_id for summary in list_unfinished(workspace.runs_root, now=_NOW)] == [
        real.run_id
    ]
    assert _victim_files(home) == before | {
        RUN_MANIFEST_FILENAME: (home / RUN_MANIFEST_FILENAME).read_bytes()
    }


def test_cleanup_refuses_a_stage_directory_pointing_at_source_data(
    workspace: Workspace, tmp_path: Path
) -> None:
    home = _source_home(tmp_path)
    before = _victim_files(home)
    manifest = _start(workspace)
    _advance_through(workspace, manifest, StageId.REVIEWED_SUBMISSION, at=_NOW)
    manifest.status = advance_run(manifest.status, RunStatus.REVIEW)
    manifest.status = advance_run(manifest.status, RunStatus.COMPLETED)
    save_manifest(manifest, root=workspace.runs_root)
    run_directory = _run_directory(workspace, manifest.run_id)
    shutil.rmtree(run_directory / "stages")
    (run_directory / "stages").symlink_to(home / ".claude", target_is_directory=True)

    with pytest.raises(RunStoreError, match="symlink") as excinfo:
        cleanup_completed(run_directory)
    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_UNSAFE_CLEANUP_PATH"
    assert _victim_files(home) == before


def test_cleanup_refuses_a_directory_that_is_not_a_run(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _victim_files(home)

    with pytest.raises(RunStoreError) as excinfo:
        cleanup_completed(home)

    assert excinfo.value.diagnostic is not None
    assert excinfo.value.diagnostic.code == "STATE_CHECKPOINT_CORRUPT"
    assert _victim_files(home) == before


def test_retention_touches_nothing_outside_the_run_directory(
    workspace: Workspace, tmp_path: Path
) -> None:
    home = _source_home(tmp_path)
    victim_before = _victim_files(home)
    stale = _start(workspace)
    _advance_through(workspace, stale, StageId.ELIGIBLE_ENGLISH, at=_NOW - timedelta(days=31))
    live = _start(workspace)
    _advance_through(workspace, live, StageId.CANDIDATE_UTTERANCES, at=_NOW)
    neighbor = workspace.runs_root.parent / "calibration"
    neighbor.mkdir(parents=True, exist_ok=True)
    (neighbor / "local-history.jsonl").write_text("{}\n", encoding="utf-8")
    loose = workspace.runs_root / "not-a-run.txt"
    loose.write_text("keep", encoding="utf-8")

    assert expire_stale_runs(workspace.runs_root, now=_NOW) == [stale.run_id]

    assert _victim_files(home) == victim_before
    assert (neighbor / "local-history.jsonl").is_file()
    assert loose.is_file()
    assert (workspace.repo / ".gitignore").is_file()
    assert stage_dir(live.run_id, StageId.CANDIDATE_UTTERANCES, root=workspace.runs_root).is_dir()
    assert not (_run_directory(workspace, stale.run_id) / "stages").exists()
