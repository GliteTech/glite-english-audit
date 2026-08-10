"""CLI: end a run and apply completed-run retention.

Run: ``uv run python -m glite_english_audit.pipeline.complete_run
--run-id <id> --outcome completed``

Step 13 of the run skill promises that when a run completes, the extracted
source text goes immediately -- not in thirty days, immediately -- leaving only
the privacy-safe package and the manifest. ``cleanup_completed`` implements
exactly that and refuses to touch a run that is not finished. Nothing called
it, and nothing set a run to ``completed`` either: ``record_step`` advances as
far as ``review`` and stops.

So every finished audit stayed ``review`` for ever. Two consequences, both
already visible in a real session. The learner's own sentences survived until
the thirty-day sweep reached them, when the product said they would be deleted
as soon as the run ended. And ``list_unfinished`` kept offering completed audits
as resumable, because a run in ``review`` is by definition unfinished.

This is the missing end of the pipeline. It is a separate command rather than a
side effect of the review page because the outcome is the user's: they may send,
they may download and upload later, and either way the run is over -- but the
page can be closed without a decision, and a run nobody finished is exactly what
resume exists for.
"""

import argparse
import json
import sys
from pathlib import Path

from glite_english_audit.artifacts.enums import RunStatus
from glite_english_audit.paths import run_dir
from glite_english_audit.state.machine import advance_run
from glite_english_audit.state.run_store import (
    RunStoreError,
    cleanup_completed,
    load_manifest,
    save_manifest,
)

_OUTCOMES = {
    "completed": RunStatus.COMPLETED,
    "completed-with-exclusions": RunStatus.COMPLETED_WITH_EXCLUSIONS,
}


def complete_run(
    run_id: str,
    *,
    outcome: RunStatus = RunStatus.COMPLETED,
    root: Path | None = None,
) -> dict[str, object]:
    """Mark the run finished, then delete what completion retires.

    The order matters and is not interchangeable. ``cleanup_completed`` refuses
    any run that is not already finished, so the status is written first; and
    the status is written to disk before the deletion rather than after, so a
    crash between the two leaves a completed run with its text still present --
    recoverable by running this again -- rather than a stripped run that still
    claims to be in review.
    """
    if outcome not in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS):
        msg = f"{outcome.value!r} is not a completion outcome"
        raise ValueError(msg)

    manifest = load_manifest(run_id, root=root)
    already = manifest.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_EXCLUSIONS)
    if not already:
        manifest.status = advance_run(manifest.status, outcome)
        save_manifest(manifest, root=root)

    directory = (root / run_id) if root is not None else run_dir(run_id)
    cleanup_completed(directory)
    return {
        "run_id": run_id,
        "status": manifest.status.value,
        "already_completed": already,
        "private_text_removed": not (directory / "steps").exists(),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the outcome; never source text."""
    parser = argparse.ArgumentParser(
        description="End a run and delete what completed-run retention retires"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--outcome",
        default="completed",
        choices=sorted(_OUTCOMES),
        help=(
            "completed-with-exclusions when the user withheld records on the "
            "review page; the count of withheld records is part of the package, "
            "and the status is what records that they withheld any"
        ),
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)

    try:
        report = complete_run(
            arguments.run_id,
            outcome=_OUTCOMES[arguments.outcome],
            root=arguments.runs_root,
        )
    except (RunStoreError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 1

    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
