"""CLI: step b — remove duplicate messages, keeping one copy.

Run: ``uv run python -m glite_english_audit.pipeline.deduplicate --run-id <id>``

Reads every session file from step a, removes messages that appear more than
once across the run, and writes the same file set to step b with one copy kept.
No model is involved: deduplication is comparison, not judgment.

**It must be a global pass, not a per-file one.** ``normalization/dedup`` exists
to collapse the same production event recorded by two different applications —
dictated in Wispr Flow, pasted into a coding agent seconds later. Those two
copies have different session identifiers by construction, so a pass that only
ever sees one file at a time can never compare them. Both would survive, both
would be counted, and the word count is the denominator of every rate this
product reports.

Running before the model steps rather than after them is the other half of the
point. The old pipeline deduplicated after authorship had already been judged,
so a model spent tokens deciding which words a person wrote in messages that
were then discarded.

The surviving copy is the earliest by timestamp, ties broken by utterance ID, so
two runs over the same data keep the same copy and produce the same counts.

Removals are recorded per file. Step b's session files are no longer a faithful
record of what that session contained, and the sidecar is what makes the
difference auditable rather than silent.
"""

import argparse
import json
import sys
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StageStatus, StepId
from glite_english_audit.artifacts.io import ensure_private_dir, write_jsonl_models
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline.record_stage import advance_to
from glite_english_audit.sessions import (
    read_all,
    read_index,
    write_index,
)

REMOVED_NAME = "removed.json"
PRODUCER_NAME = "pipeline.deduplicate"


def deduplicate(run_id: str, *, runs_root: Path | None = None) -> dict[str, object]:
    """Run step b for one run. Returns aggregate counts only."""
    source = step_dir(run_id, StepId.A_COLLECTED, root=runs_root)
    target = ensure_private_dir(step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root))

    per_file = list(read_all(source))
    if not per_file:
        msg = f"step a has no session files in {source.name}; run collect first"
        raise ValueError(msg)

    # Reassemble the whole run, deduplicate once, then redistribute. The
    # reassembly is what makes the cross-application case reachable at all.
    everything = [utterance for _, members in per_file for utterance in members]
    outcome = dedupe(everything)
    survivors = {utterance.utterance_id for utterance in outcome.canonical}

    removed: dict[str, list[str]] = {}
    kept_total = 0
    for path, members in per_file:
        kept = [utterance for utterance in members if utterance.utterance_id in survivors]
        dropped = [u.utterance_id for u in members if u.utterance_id not in survivors]
        kept_total += write_jsonl_models(target / path.name, kept)
        if dropped:
            removed[path.name] = sorted(dropped)

    # Same names in, same names out — including a session whose every message
    # was a duplicate, which stays as an empty file rather than disappearing.
    # A missing file and an emptied one mean different things, and only one of
    # them is what happened.
    write_index(target, read_index(source))
    (target / REMOVED_NAME).write_text(
        json.dumps({"removed": removed}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    advance_to(
        run_id,
        StepId.B_DEDUPLICATED,
        StageStatus.PROMOTED,
        producer_version=CLIENT_VERSION,
        runs_root=runs_root,
    )
    return {
        "sessions": len(per_file),
        "messages_in": len(everything),
        "messages_out": kept_total,
        "removed": len(everything) - kept_total,
        "sessions_affected": len(removed),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Step b: remove duplicate messages")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    result = deduplicate(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
