"""Local source inventory: the only discovery output the agent may see.

Running this module prints one JSON document containing
:class:`InstanceInventorySummary` rows — opaque labels and aggregate numbers,
never text, paths, or workspace names (specification, 2.3-2.4). The private
:class:`SourceInstanceRecord` rows and the label-to-path map are written to
the private run directory when ``--run-dir`` is given; they never reach
stdout.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import InstanceInventorySummary, SourceInstanceRecord
from glite_english_audit.discovery.base import DiscoveryContext, DiscoveryOutcome
from glite_english_audit.discovery.registry import create_all_adapters
from glite_english_audit.paths import detect_os_environment, stage_dir


class PrivateInventory(BaseModel):
    """Private discovery result persisted inside the run directory."""

    model_config = ConfigDict(extra="forbid")

    records: list[SourceInstanceRecord]
    instance_paths: dict[str, str]


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


def run_discovery(context: DiscoveryContext) -> list[DiscoveryOutcome]:
    """Run every registered adapter. One failing adapter never stops others."""
    outcomes: list[DiscoveryOutcome] = []
    for adapter in create_all_adapters():
        try:
            outcomes.append(adapter.discover(context))
        except Exception:  # adapter isolation: one broken source never stops the rest
            outcomes.append(DiscoveryOutcome(records=[], instance_paths={}))
    return outcomes


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
    if arguments.run_dir is None and arguments.run_id is not None:
        arguments.run_dir = stage_dir(
            arguments.run_id, StageId.SOURCE_INVENTORY, root=arguments.runs_root
        )

    # Registration stays out of module import time so tests control the registry.
    from glite_english_audit import adapters

    adapters.register_all()

    context = DiscoveryContext(
        os_environment=detect_os_environment(),
        home=Path.home(),
        now=datetime.now(tz=UTC),
        environ=dict(os.environ),
    )
    outcomes = run_discovery(context)
    records = [record for outcome in outcomes for record in outcome.records]
    if arguments.run_dir is not None:
        ensure_private_dir(arguments.run_dir)
        private = PrivateInventory(
            records=records,
            instance_paths={
                key: str(path)
                for outcome in outcomes
                for key, path in outcome.instance_paths.items()
            },
        )
        write_model(arguments.run_dir / "source-inventory.json", private)

    summaries = [summarize(record) for record in records]
    payload = {"inventory": [summary.model_dump(mode="json") for summary in summaries]}
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
