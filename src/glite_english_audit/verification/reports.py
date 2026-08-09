"""Verification report artifacts.

Reports are separate append-only metadata artifacts, so verifying an artifact
never mutates it (specification, 5.2). Promotion decisions read the latest
report for the artifact hash they are about to promote.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.diagnostics.codes import Diagnostic, Severity


class VerificationReport(BaseModel):
    """Outcome of one verifier pass over one artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    run_id: str
    step_id: StepId
    artifact_id: str
    artifact_hash: str
    verifier_name: str
    verifier_version: str
    kind: Literal["deterministic", "semantic"]
    passed: bool
    diagnostics: list[Diagnostic]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "created_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @classmethod
    def passed_matches(cls, diagnostics: list[Diagnostic]) -> bool:
        """The pass flag every report must carry: no error-level diagnostics."""
        return all(diagnostic.severity is not Severity.ERROR for diagnostic in diagnostics)
