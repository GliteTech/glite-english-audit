"""CLI: start the loopback final-review server for one run.

Run: ``uv run python -m glite_english_audit.review_server --run-id <run-id>``.

Loads the reviewed submission artifact built from step e, detects whether an
optional direct API action is configured, starts the loopback server, prints
the tokenized local URL, and serves until the review completes, the user shuts
it down, or the inactivity timeout fires. Website report creation and package
download remain available without direct API configuration.
"""

import argparse
import sys
from pathlib import Path

from glite_english_audit.artifacts.io import read_model
from glite_english_audit.artifacts.models import ReviewedSubmissionArtifact
from glite_english_audit.paths import endpoint_config_dir, submission_dir
from glite_english_audit.review_server.server import start_review_server
from glite_english_audit.submission.capability import detect_capability

REVIEWED_ARTIFACT_NAME = "reviewed-submission.json"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Loopback final-review page for one run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    parser.add_argument("--config-dir", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)

    artifact_path = (
        submission_dir(arguments.run_id, root=arguments.runs_root) / REVIEWED_ARTIFACT_NAME
    )
    if not artifact_path.is_file():
        sys.stderr.write("This run has nothing to review yet. Finish the earlier steps first.\n")
        return 1
    reviewed = read_model(artifact_path, ReviewedSubmissionArtifact)
    config_dir = arguments.config_dir if arguments.config_dir is not None else endpoint_config_dir()
    capability = detect_capability(config_dir)

    handle = start_review_server(
        reviewed,
        capability,
        run_id=arguments.run_id,
        runs_root=arguments.runs_root,
    )
    thread = handle.serve_forever_in_thread()
    sys.stdout.write(
        "Review page ready. Open this address in your browser:\n"
        f"{handle.url}\n"
        "The page is local-only and stops after 30 minutes without activity.\n"
    )
    sys.stdout.flush()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        handle.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
