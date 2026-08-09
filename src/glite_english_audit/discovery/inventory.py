"""Local source inventory: the only discovery output the agent may see.

Running this module prints one JSON document containing
:class:`InstanceInventorySummary` rows — opaque labels and aggregate numbers,
never text, paths, or workspace names (specification, 2.3-2.4). The private
:class:`SourceInstanceRecord` rows and the label-to-path map are written to
the private run directory when ``--run-dir`` is given; they never reach
stdout.
"""

import argparse
import functools
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import InstanceInventorySummary, SourceInstanceRecord
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery.base import DiscoveryContext, DiscoveryOutcome
from glite_english_audit.discovery.parallel import map_in_threads, thread_count
from glite_english_audit.discovery.registry import adapter_ids, create_adapter
from glite_english_audit.paths import (
    detect_os_environment,
    pending_inventory_dir,
    stage_dir,
)


class PrivateInventory(BaseModel):
    """Private discovery result persisted inside the run directory."""

    model_config = ConfigDict(extra="forbid")

    records: list[SourceInstanceRecord]
    instance_paths: dict[str, str]
    failures: list[Diagnostic] = []


@dataclass(frozen=True)
class DiscoveryReport:
    """One discovery pass: what every adapter produced, and which ones failed."""

    outcomes: list[DiscoveryOutcome]
    failures: list[Diagnostic]


def summarize(record: SourceInstanceRecord) -> InstanceInventorySummary:
    """Project a private instance record onto its agent-visible summary."""
    return InstanceInventorySummary(
        adapter_id=record.adapter_id,
        adapter_version=record.adapter_version,
        opaque_label=record.opaque_label,
        stability=record.stability,
        accessibility=record.accessibility,
        diagnostic_code=record.diagnostic_code,
        estimated_records=record.estimated_records,
        earliest_timestamp=record.earliest_timestamp,
        latest_timestamp=record.latest_timestamp,
        candidate_messages=record.candidate_messages,
        candidate_words=record.candidate_words,
        candidate_bytes=record.candidate_bytes,
    )


@dataclass(frozen=True)
class _AdapterResult:
    """What one adapter thread produced: an outcome, or the failure's type."""

    adapter_id: str
    outcome: DiscoveryOutcome | None
    failure_type: str | None


def _discover_one(adapter_id: str, *, context: DiscoveryContext) -> _AdapterResult:
    """Build and run one adapter, converting any failure into a result.

    Only the exception's type name is kept. An adapter message could quote the
    source data that broke it, and no source text may reach a diagnostic.
    """
    try:
        outcome = create_adapter(adapter_id).discover(context)
    except Exception as error:  # adapter isolation: one broken source never stops the rest
        return _AdapterResult(
            adapter_id=adapter_id, outcome=None, failure_type=type(error).__name__
        )
    return _AdapterResult(adapter_id=adapter_id, outcome=outcome, failure_type=None)


def run_discovery(context: DiscoveryContext) -> DiscoveryReport:
    """Run every registered adapter concurrently, isolated from each other.

    Adapters run on threads: each one spends its time in filesystem walks and
    in the shared worker pool, so threads add concurrency without stacking a
    second layer of processes. Results keep registry order, never completion
    order, so the inventory is identical whatever the thread count.
    """
    ids = adapter_ids()
    results = map_in_threads(
        functools.partial(_discover_one, context=context),
        ids,
        workers=thread_count(item_count=len(ids), environ=context.environ),
    )
    return DiscoveryReport(
        outcomes=[result.outcome for result in results if result.outcome is not None],
        failures=[
            Diagnostic.from_code(
                "SOURCE_DISCOVERY_FAILED",
                f"discovery raised {result.failure_type}; the other sources continued",
                item_ref=result.adapter_id,
            )
            for result in results
            if result.failure_type is not None
        ],
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints agent-safe summaries as JSON."""
    parser = argparse.ArgumentParser(description="Local source inventory (aggregate-only output)")
    parser.add_argument(
        "--run-id",
        default=None,
        help="run whose stage-0 directory receives the full private records",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="explicit private directory override for the full records (tests)",
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    if arguments.run_dir is None:
        if arguments.run_id is not None:
            arguments.run_dir = stage_dir(
                arguments.run_id, StageId.SOURCE_INVENTORY, root=arguments.runs_root
            )
        else:
            # No run exists yet at stage 0, so the inventory waits in the
            # pending location until start_run adopts it.
            arguments.run_dir = pending_inventory_dir()

    # Registration stays out of module import time so tests control the registry.
    from glite_english_audit import adapters

    adapters.register_all()

    context = DiscoveryContext(
        os_environment=detect_os_environment(),
        home=Path.home(),
        now=datetime.now(tz=UTC),
        environ=dict(os.environ),
    )
    report = run_discovery(context)
    records = [record for outcome in report.outcomes for record in outcome.records]
    if arguments.run_dir is not None:
        ensure_private_dir(arguments.run_dir)
        private = PrivateInventory(
            records=records,
            instance_paths={
                key: str(path)
                for outcome in report.outcomes
                for key, path in outcome.instance_paths.items()
            },
            failures=report.failures,
        )
        write_model(arguments.run_dir / "source-inventory.json", private)

    summaries = [summarize(record) for record in records]
    payload = {"inventory": [summary.model_dump(mode="json") for summary in summaries]}
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
