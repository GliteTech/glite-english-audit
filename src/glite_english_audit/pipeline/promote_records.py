"""CLI: stage 6 to 7 — scan safe-record candidates and promote the approved.

Run: ``uv run python -m glite_english_audit.pipeline.promote_records --run-id <id>``

This is the deterministic half of the double protection. A candidate is
promoted only when the scanner reports nothing; anything it flags is withheld
with a non-descriptive reason code, and the count of withheld records is all
Glite ever learns about them (specification, 5.6, 8.3).

The independent semantic confidentiality verifier is the other half and runs
as its own skill. Promotion here does not substitute for it: the orchestration
requires both before a record reaches the review page.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StageId, StageStatus
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import SafeRecordCandidate
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline.record_stage import advance_to
from glite_english_audit.verification.confidentiality_report import load_report
from glite_english_audit.verification.privacy_scanner import scan_safe_record
from glite_english_audit.verification.reports import VerificationReport

CANDIDATES_NAME = "candidates.jsonl"
APPROVED_NAME = "approved.jsonl"
WITHHELD_NAME = "withheld.json"
REPORT_NAME = "privacy-scan-report.json"
MANIFEST_NAME = "approved-manifest.json"
PRODUCER_NAME = "pipeline.promote_records"


class ApprovedRecordsManifest(BaseModel):
    """Stage-7 manifest for the promoted privacy-approved records."""

    model_config = ConfigDict(extra="forbid")

    envelope: ArtifactEnvelope
    record_count: int = Field(ge=0)
    jsonl_relative_path: str
    jsonl_sha256: str

    @field_validator("jsonl_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            msg = "jsonl_sha256 must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value


def _envelope(run_id: str, schema_name: str) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        schema_name=schema_name,
        schema_version=1,
        artifact_id=new_artifact_id(),
        run_id=run_id,
        stage_id=StageId.PRIVACY_APPROVED,
        producer_name=PRODUCER_NAME,
        producer_version=CLIENT_VERSION,
        created_at=utc_now(),
    )


def promote(run_id: str, *, runs_root: Path | None = None) -> dict[str, object]:
    """Scan every candidate and split approved from withheld."""
    source_dir = stage_dir(run_id, StageId.SAFE_RECORDS, root=runs_root)
    target_dir = ensure_private_dir(stage_dir(run_id, StageId.PRIVACY_APPROVED, root=runs_root))
    candidates = list(read_jsonl_models(source_dir / CANDIDATES_NAME, SafeRecordCandidate))

    # The other half of the double protection (specification, 6.6). Loading it
    # here rather than trusting it ran is the difference between an attestation
    # and a claim: without this, skipping the semantic verifier produced a
    # package byte-identical to one that passed both gates.
    confidentiality = load_report(run_id, runs_root=runs_root)
    cleared = confidentiality.passed_ids()

    approved: list[SafeRecordCandidate] = []
    withheld: dict[str, list[str]] = {}
    diagnostics = []
    for candidate in candidates:
        if candidate.creation_failed:
            withheld[candidate.mistake_id] = [
                candidate.failure_reason_code or "WITHHELD_PRIVACY_UNSAFE"
            ]
            continue
        found = scan_safe_record(candidate.record, item_ref=candidate.mistake_id)
        if found:
            withheld[candidate.mistake_id] = sorted({d.code for d in found})
            diagnostics.extend(found)
        elif candidate.mistake_id not in cleared:
            # Either the semantic verifier failed it, or the report never named
            # it. Both are withheld: a candidate nobody judged is not a
            # candidate that passed.
            withheld[candidate.mistake_id] = ["WITHHELD_PRIVACY_UNSAFE"]
        else:
            approved.append(candidate)

    approved_path = target_dir / APPROVED_NAME
    write_jsonl_models(approved_path, approved)
    digest = sha256_hex(approved_path.read_bytes())
    (target_dir / WITHHELD_NAME).write_text(
        json.dumps(withheld, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_model(
        target_dir / REPORT_NAME,
        VerificationReport(
            report_id=new_artifact_id(),
            run_id=run_id,
            stage_id=StageId.PRIVACY_APPROVED,
            artifact_id=new_artifact_id(),
            artifact_hash=digest,
            verifier_name="privacy_scanner",
            verifier_version=CLIENT_VERSION,
            kind="deterministic",
            passed=not diagnostics,
            diagnostics=diagnostics,
            created_at=utc_now(),
        ),
    )
    write_model(
        target_dir / MANIFEST_NAME,
        ApprovedRecordsManifest(
            envelope=_envelope(run_id, "privacy_approved_records"),
            record_count=len(approved),
            jsonl_relative_path=APPROVED_NAME,
            jsonl_sha256=digest,
        ),
    )
    # Stages 5 and 6 produced the input this stage just re-read and approved,
    # so both are durable; stage 7 is promoted on its own scanner report. All
    # three are semantic stages, and the confidentiality skill that runs before
    # this command is their second reader (specification, 6.6).
    for stage in (StageId.PRIVATE_MISTAKES, StageId.SAFE_RECORDS, StageId.PRIVACY_APPROVED):
        advance_to(
            run_id,
            stage,
            StageStatus.PROMOTED,
            producer_version=CLIENT_VERSION,
            runs_root=runs_root,
        )
    return {
        "candidates": len(candidates),
        "approved": len(approved),
        "withheld_for_privacy": len(withheld),
        "withheld_reason_codes": sorted({code for codes in withheld.values() for code in codes}),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Stage 6-7: scan and promote safe records")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    result = promote(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
