"""CLI: stages 1 and 2 — snapshot the selected sources and extract candidates.

Run: ``uv run python -m glite_english_audit.pipeline.collect --run-id <run-id>``

For every selected instance this creates a consistent read-only snapshot under
the repository-owned snapshot tree, extracts candidate user-authored
utterances from that snapshot only, and applies the run's period and
record-cutoff bounds. Snapshots are removed as soon as extraction for their
instance has been written, because nothing downstream needs them
(specification, 3.6).

One failing source never stops the others: it is recorded and skipped, and the
run continues with a reported exclusion (specification, 9.5).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.manifest import RunManifest
from glite_english_audit.artifacts.models import (
    CandidateUtterancesManifest,
    NormalizedUtterance,
    SnapshotManifest,
    SourceInstanceRecord,
)
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.discovery.registry import create_adapter
from glite_english_audit.discovery.snapshot_safety import cleanup_snapshot, ensure_safe_snapshot_dir
from glite_english_audit.paths import run_dir, stage_dir

INVENTORY_NAME = "source-inventory.json"
MANIFEST_NAME = "run-manifest.json"
CANDIDATES_NAME = "candidates.jsonl"
CANDIDATES_MANIFEST_NAME = "candidate-utterances-manifest.json"
PRODUCER_NAME = "pipeline.collect"


def _within_bounds(
    utterance: NormalizedUtterance, *, start: datetime, end: datetime, cutoff: datetime
) -> bool:
    """Keep undated records; bound dated ones by the period and the cutoff."""
    stamp = utterance.timestamp
    if stamp is None:
        return True
    return start <= stamp <= min(end, cutoff)


def collect(
    run_id: str, *, runs_root: Path | None = None, repo: Path | None = None
) -> dict[str, object]:
    """Run stages 1 and 2 for one run. Returns aggregate counts only."""
    base = runs_root / run_id if runs_root is not None else run_dir(run_id)
    manifest = read_model(base / MANIFEST_NAME, RunManifest)
    selection = manifest.selection
    if selection is None:
        msg = "the run has no confirmed selection; run start_run first"
        raise ValueError(msg)

    # Registering here rather than at import time keeps the registry under a
    # test's control, but this driver runs in its own process and must populate
    # it: without this every adapter lookup raises and every source is excluded.
    from glite_english_audit import adapters

    adapters.register_all()

    inventory_dir = stage_dir(run_id, StageId.SOURCE_INVENTORY, root=runs_root)
    inventory = read_model(inventory_dir / INVENTORY_NAME, PrivateInventory)
    by_key: dict[str, SourceInstanceRecord] = {r.instance_key: r for r in inventory.records}

    snapshot_root = ensure_safe_snapshot_dir(run_id, repo=repo)
    snapshot_stage = ensure_private_dir(stage_dir(run_id, StageId.SOURCE_SNAPSHOTS, root=runs_root))
    candidates_stage = ensure_private_dir(
        stage_dir(run_id, StageId.CANDIDATE_UTTERANCES, root=runs_root)
    )

    utterances: list[NormalizedUtterance] = []
    per_source: dict[str, int] = {}
    excluded: list[dict[str, str]] = []

    for instance_key in selection.selected_instance_keys:
        record = by_key.get(instance_key)
        source_path = inventory.instance_paths.get(instance_key)
        if record is None or source_path is None:
            excluded.append({"instance": instance_key[:12], "reason": "LINEAGE_MISSING_INPUT"})
            continue
        try:
            adapter = create_adapter(record.adapter_id)
            relative_dir = f"{record.adapter_id}/{instance_key[:12]}"
            target = snapshot_root / relative_dir
            ensure_private_dir(target)
            capture = adapter.snapshot(record, Path(source_path), target)
            snapshot_manifest = SnapshotManifest(
                envelope=ArtifactEnvelope(
                    schema_name="snapshot_manifest",
                    schema_version=1,
                    artifact_id=new_artifact_id(),
                    run_id=run_id,
                    stage_id=StageId.SOURCE_SNAPSHOTS,
                    producer_name=PRODUCER_NAME,
                    producer_version=CLIENT_VERSION,
                    created_at=utc_now(),
                ),
                adapter_id=record.adapter_id,
                instance_key=instance_key,
                snapshot_relative_dir=relative_dir,
                # An adapter reports paths relative to the directory it was
                # given; the manifest's own contract is that every entry
                # resolves under the run's snapshot root, which is where
                # cleanup looks. Rebase them here or cleanup deletes nothing.
                files=[
                    entry.model_copy(
                        update={"relative_path": f"{relative_dir}/{entry.relative_path}"}
                    )
                    for entry in capture.files
                ],
            )
            write_model(snapshot_stage / f"{instance_key[:12]}.json", snapshot_manifest)

            kept = 0
            for utterance in adapter.extract(record, target):
                if _within_bounds(
                    utterance,
                    start=selection.period.start,
                    end=selection.period.end,
                    cutoff=selection.record_cutoff_at,
                ):
                    utterances.append(utterance)
                    kept += 1
            per_source[record.adapter_id] = per_source.get(record.adapter_id, 0) + kept
            # The extraction for this instance is durable, so the snapshot has
            # no remaining dependent and is removed immediately.
            cleanup_snapshot(snapshot_manifest, run_id, repo=repo)
        except Exception as error:  # one source never stops the others
            excluded.append({"instance": instance_key[:12], "reason": error.__class__.__name__})

    # Deterministic order: dated records by time, undated ones last, ties by ID.
    def _order(utterance: NormalizedUtterance) -> tuple[int, float, str]:
        if utterance.timestamp is None:
            return (1, 0.0, utterance.utterance_id)
        return (0, utterance.timestamp.timestamp(), utterance.utterance_id)

    utterances.sort(key=_order)
    candidates_path = candidates_stage / CANDIDATES_NAME
    count = write_jsonl_models(candidates_path, utterances)
    write_model(
        candidates_stage / CANDIDATES_MANIFEST_NAME,
        CandidateUtterancesManifest(
            envelope=ArtifactEnvelope(
                schema_name="candidate_utterances",
                schema_version=1,
                artifact_id=new_artifact_id(),
                run_id=run_id,
                stage_id=StageId.CANDIDATE_UTTERANCES,
                producer_name=PRODUCER_NAME,
                producer_version=CLIENT_VERSION,
                created_at=utc_now(),
            ),
            utterance_count=count,
            jsonl_relative_path=CANDIDATES_NAME,
            jsonl_sha256=sha256_hex(candidates_path.read_bytes()),
        ),
    )
    return {
        "candidate_utterances": count,
        "per_source": per_source,
        "excluded_instances": excluded,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Stages 1-2: snapshot and extract candidates")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    parser.add_argument("--repo", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    result = collect(arguments.run_id, runs_root=arguments.runs_root, repo=arguments.repo)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
