"""CLI: how many mistakes step d has found so far, and whether that is enough.

Run: ``uv run python -m glite_english_audit.pipeline.found_count --run-id <id>``

The audit stops on evidence, not on a calendar. Step d works through the
session files newest first, and after each file the run skill asks this driver
whether the report has what it needs; once it does, the older sessions are
left unanalyzed and the review reports them as unread coverage rather than
pretending they were read.

Counts only. The output never contains a record, a span, or a word the
learner wrote -- it is printed into the conversation, and the conversation is
not the store.
"""

import argparse
import json
import sys
from pathlib import Path

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.paths import step_dir

# Enough found mistakes to stop reading older sessions. The report is designed
# for 200-300 verified mistakes, and verification measurably rejects about a
# seventh of what step d finds, so 240 found lands near the bottom of that
# band. Below it the report thins; far above it the grouping step measurably
# degrades -- a 1,027-mistake package defeated three generations of it.
TARGET_FOUND_MISTAKES: int = 240


def count_found(run_id: str, *, runs_root: Path | None = None) -> dict[str, int | bool]:
    """Sum step d's drafts without reading their content.

    Counts the agent drafts (``agent/session-NNNN.out.jsonl``), not the
    promoted files: the whole point is to be askable between batches, before
    ``--apply`` has run. One JSONL line is one draft, so counting nonempty
    lines is the whole job -- parsing would drag the learner's text through
    this driver for nothing.
    """
    directory = step_dir(run_id, StepId.D_MISTAKES, root=runs_root) / "agent"
    found = 0
    analyzed = 0
    for path in sorted(directory.glob("session-*.out.jsonl")) if directory.is_dir() else []:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        found += len(lines)
        analyzed += 1
    return {
        "found": found,
        "sessions_analyzed": analyzed,
        "target": TARGET_FOUND_MISTAKES,
        "enough": found >= TARGET_FOUND_MISTAKES,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Count step d's found mistakes")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    report = count_found(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
