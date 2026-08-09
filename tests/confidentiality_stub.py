"""A stand-in for the independent semantic confidentiality verifier.

That verifier is a model, so tests cannot run it. They can, and must, produce
the report it would leave: `pipeline/promote_records` refuses to promote a
candidate the report does not clear, which is what stops a run that skipped the
verifier from producing a package indistinguishable from one that passed both
gates.

Using this helper rather than relaxing the requirement keeps the tests
exercising the real promotion path.
"""

import json
from pathlib import Path

from glite_english_audit.artifacts.io import ensure_private_dir
from glite_english_audit.verification.confidentiality_report import (
    REPORT_TYPE,
    REPORT_VERSION,
    report_path,
)

STUB_VERIFIER_VERSION = "1.0.0"


def write_confidentiality_report(
    run_id: str,
    passed_ids: list[str],
    *,
    failed_ids: list[str] | None = None,
    runs_root: Path | None = None,
) -> Path:
    """Write a report clearing ``passed_ids`` and failing ``failed_ids``."""
    failed = failed_ids or []
    results: list[dict[str, object]] = [
        {"mistake_id": mistake_id, "verdict": "pass"} for mistake_id in passed_ids
    ]
    results += [
        {
            "mistake_id": mistake_id,
            "verdict": "fail",
            "diagnostics": [
                {
                    "code": "PRIVACY_REIDENTIFICATION_RISK",
                    "field": "record",
                    "note": "the combination of details could identify one team",
                }
            ],
        }
        for mistake_id in failed
    ]
    target = report_path(run_id, runs_root=runs_root)
    ensure_private_dir(target.parent)
    target.write_text(
        json.dumps(
            {
                "report_type": REPORT_TYPE,
                "report_version": REPORT_VERSION,
                "results": results,
                "counts": {
                    "checked": len(results),
                    "passed": len(passed_ids),
                    "failed": len(failed),
                },
                "systemic_failure": False,
                "verifier_version": STUB_VERIFIER_VERSION,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target
