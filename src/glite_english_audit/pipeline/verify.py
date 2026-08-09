"""CLI: step e — confirm step d's records, and fail loudly if it did more.

Run: ``uv run python -m glite_english_audit.pipeline.verify --run-id <id>
--apply``

Step e is a second reader, one agent per session file, and in normal operation
it changes nothing. Step d is required to emit records that are already clean,
so a run with step e removed should still be right; this step exists to catch
the rare record that reads badly to fresh eyes.

The one thing that would make removing step e unsafe is it becoming a second
author. That used to be checked: the driver compared each e file against its d
file and reported a record added, altered, repeated or moved. It is now
unrepresentable. The agent returns the indices to drop and the driver rebuilds
the file from step d's own records, so there is no path by which a step-e file
carries a record step d did not write. Four checks were deleted because their
failures cannot occur, not because they stopped mattering.

That also closes a gap the old shape left. The skill demanded byte-identical
copies while the driver only compared parsed models, so a re-serialized record
passed a check the instructions said it should fail.

Deletions are recorded per file. An e file is no longer a faithful copy of its d
file, and the difference is exactly this run's withheld-for-privacy count.

There is no ``--prepare``: step e has nothing to decide before it starts, and
promoting step d creates its directory, writes the projection its agents read,
and names the file each of them writes.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StepId, StepStatus
from glite_english_audit.artifacts.hashing import new_artifact_id
from glite_english_audit.artifacts.io import write_jsonl_models
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline.agent_io import (
    DropList,
    expand_verified,
    verdict_path,
)
from glite_english_audit.pipeline.mistakes import (
    read_records,
    step_digest,
    write_step_report,
)
from glite_english_audit.pipeline.record_step import advance_to, mark_failed
from glite_english_audit.sessions import session_files
from glite_english_audit.state.run_store import load_manifest

STEP = StepId.E_VERIFIED
SOURCE_STEP = StepId.D_MISTAKES
DROPPED_NAME = "dropped.json"
PRODUCER_NAME = "pipeline.verify"
SKILL_NAME = "verify-mistake-confidentiality"


class VerificationOutcome(BaseModel):
    """What ``--apply`` found, in counts and diagnostic codes only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    sessions: int = Field(ge=0)
    records_in: int = Field(ge=0)
    records_kept: int = Field(ge=0)
    records_dropped: int = Field(ge=0)
    sessions_affected: int = Field(ge=0)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


def read_drop_list(path: Path, *, item_ref: str) -> tuple[DropList, list[Diagnostic]]:
    """Parse one agent-written step-e verdict: the indices it will not share.

    One object for the whole session rather than one line per record. A file
    that cannot be read at all is an empty verdict plus a diagnostic, so the
    step fails rather than quietly sharing everything.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return DropList(), [
            Diagnostic.from_code(
                "SCHEMA_INVALID_JSON", "a step-e verdict is not valid JSON", item_ref=item_ref
            )
        ]
    try:
        return DropList.model_validate(payload), []
    except ValidationError:
        return DropList(), [
            Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "a step-e verdict does not validate as a list of indices to drop",
                item_ref=item_ref,
            )
        ]


def _require_promoted_source(run_id: str, *, runs_root: Path | None = None) -> None:
    """Refuse to confirm records nothing has validated yet."""
    manifest = load_manifest(run_id, root=runs_root)
    if manifest.steps[SOURCE_STEP].status is not StepStatus.PROMOTED:
        msg = (
            "step d is not promoted, so these records have not been checked against the "
            "text they cite; run pipeline.mistakes --apply until it exits zero first"
        )
        raise ValueError(msg)


def apply_verification(run_id: str, *, runs_root: Path | None = None) -> VerificationOutcome:
    """Check every agent-written step-e file, then promote or refuse."""
    _require_promoted_source(run_id, runs_root=runs_root)
    source = step_dir(run_id, SOURCE_STEP, root=runs_root)
    target = step_dir(run_id, STEP, root=runs_root)
    inputs = session_files(source)
    if not inputs:
        msg = f"step d has no session files in {source.name}; run step d first"
        raise ValueError(msg)

    diagnostics: list[Diagnostic] = []
    dropped: dict[str, list[str]] = {}
    records_in = 0
    records_kept = 0
    for path in inputs:
        produced, produced_diagnostics = read_records(path)
        # A promoted step-d file that no longer parses was edited after its own
        # check passed, so this step is the one that must notice.
        diagnostics.extend(produced_diagnostics)
        records_in += len(produced)

        answers = verdict_path(target, path.name)
        if not answers.is_file():
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_MISSING_INPUT",
                    "step d has a session file step e did not answer; a session that shares "
                    "nothing is an empty file, not a missing one",
                    item_ref=path.name,
                )
            )
            continue
        verdict, verdict_diagnostics = read_drop_list(answers, item_ref=path.name)
        confirmed, expansion = expand_verified(produced, verdict, item_ref=path.name)
        session_diagnostics = [*verdict_diagnostics, *expansion]
        diagnostics.extend(session_diagnostics)
        if session_diagnostics:
            # A rejected verdict withheld nothing, because nothing was decided.
            # Counting its whole session as dropped would put a number in front
            # of an operator that says the run withheld everything, when what
            # happened is that one file could not be read.
            continue
        write_jsonl_models(target / path.name, confirmed)
        records_kept += len(confirmed)

        kept_ids = {record.record_id for record in confirmed}
        gone = [record.record_id for record in produced if record.record_id not in kept_ids]
        if gone:
            dropped[path.name] = gone

    expected = {path.name for path in inputs}
    for path in session_files(target):
        if path.name not in expected:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    "step e has a session file step d does not, so the two steps describe "
                    "different runs",
                    item_ref=path.name,
                )
            )

    artifact_id = new_artifact_id()
    report = write_step_report(
        run_id,
        STEP,
        verifier_name=PRODUCER_NAME,
        skill_name=SKILL_NAME,
        artifact_id=artifact_id,
        artifact_hash=step_digest(target),
        diagnostics=diagnostics,
        runs_root=runs_root,
    )
    outcome = VerificationOutcome(
        passed=report.passed,
        sessions=len(inputs),
        records_in=records_in,
        records_kept=records_kept,
        # Counted from the records actually absent from step e rather than from
        # the difference of two totals: a file that added a record would make
        # that difference negative and this report unreportable, and a contract
        # violation must arrive as a diagnostic rather than as a crash.
        records_dropped=sum(len(gone) for gone in dropped.values()),
        sessions_affected=len(dropped),
        diagnostics=diagnostics,
    )
    if not report.passed:
        mark_failed(run_id, STEP, quarantined=True, runs_root=runs_root)
        return outcome

    (target / DROPPED_NAME).write_text(
        json.dumps({"dropped": dropped}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    advance_to(
        run_id,
        STEP,
        StepStatus.PROMOTED,
        artifact_id=artifact_id,
        artifact_hash=report.artifact_hash,
        producer_version=CLIENT_VERSION,
        runs_root=runs_root,
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exits non-zero when step e did more than drop records."""
    parser = argparse.ArgumentParser(description="Step e: confirm the mistake records")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="check every agent-written file and promote step e",
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    outcome = apply_verification(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(json.dumps(outcome.model_dump(mode="json"), indent=2) + "\n")
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
