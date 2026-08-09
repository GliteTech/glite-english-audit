"""Read and enforce the independent confidentiality verifier's report.

The product's central privacy claim is double protection: a deterministic
scanner and an independent semantic verifier must both clear a record before
it can be shared (specification, 6.6). The deterministic half was enforced —
``pipeline/promote_records`` runs it and withholds what it flags. The semantic
half was not enforced anywhere. No module read its report, nothing required
one to exist, and ``pipeline/build_review`` stamped
``privacy_verifier_version`` onto every approved record regardless.

The consequence was a false attestation leaving the machine. Running the
deterministic scanner, skipping the confidentiality skill entirely, and
building the package produced bytes identical to a package whose records had
passed both gates. The field Glite receives as evidence that an independent
verifier checked these records was evidence of nothing.

This module makes the report load-bearing. A candidate is promotable only when
this run's report names it with a passing verdict, and the version recorded on
the shared record comes from the report rather than from whichever client
happened to build the package.

Absence is a refusal, not a pass. A missing report means the verifier did not
run, which is exactly the case the attestation must not survive.
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.io import read_model
from glite_english_audit.paths import stage_dir

REPORT_NAME = "confidentiality-report.json"

_VERSION_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)*$")

REPORT_TYPE = "confidentiality_verification"
REPORT_VERSION = 1


class ConfidentialityDiagnostic(BaseModel):
    """One reason a record failed, naming a category and never a value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    field: Literal["mistake", "rule", "example", "record"]
    note: str


class ConfidentialityResult(BaseModel):
    """One verdict for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mistake_id: str
    verdict: Literal["pass", "fail"]
    diagnostics: list[ConfidentialityDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _diagnostics_match_verdict(self) -> "ConfidentialityResult":
        # A failure with no reason cannot be acted on, and a pass carrying
        # diagnostics is a verdict contradicting its own evidence. Either way
        # the report is not something to promote records against.
        if self.verdict == "fail" and not self.diagnostics:
            msg = f"{self.mistake_id}: a failed record must carry at least one diagnostic"
            raise ValueError(msg)
        if self.verdict == "pass" and self.diagnostics:
            msg = f"{self.mistake_id}: a passed record must carry no diagnostics"
            raise ValueError(msg)
        return self


class ConfidentialityCounts(BaseModel):
    """The report's own totals, checked against its results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)


class ConfidentialityReport(BaseModel):
    """The whole report the verify-mistake-confidentiality skill writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: Literal["confidentiality_verification"]
    report_version: int = Field(ge=1)
    results: list[ConfidentialityResult]
    counts: ConfidentialityCounts
    systemic_failure: bool = False
    verifier_version: str | None = None
    """Version of the skill that produced this report, when it stated one.

    Recorded on every record the report clears, so the attestation that
    reaches Glite names the verifier that actually ran. Held to a plain dotted
    version because it leaves the machine: the submission gate rejects any
    version field that is not one, and finding that out at materialization
    time would mean the whole run fails at the last step."""

    @field_validator("verifier_version")
    @classmethod
    def _plain_version(cls, value: str | None) -> str | None:
        if value is not None and not _VERSION_PATTERN.fullmatch(value):
            msg = f"verifier_version must be a plain dotted version, not {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _counts_match_results(self) -> "ConfidentialityReport":
        passed = sum(1 for result in self.results if result.verdict == "pass")
        failed = len(self.results) - passed
        if (self.counts.checked, self.counts.passed, self.counts.failed) != (
            len(self.results),
            passed,
            failed,
        ):
            msg = (
                f"the report's counts ({self.counts.checked} checked, {self.counts.passed} "
                f"passed, {self.counts.failed} failed) do not match its "
                f"{len(self.results)} results"
            )
            raise ValueError(msg)
        seen = {result.mistake_id for result in self.results}
        if len(seen) != len(self.results):
            msg = "the report names the same candidate more than once"
            raise ValueError(msg)
        return self

    def passed_ids(self) -> frozenset[str]:
        """Candidates this report cleared."""
        return frozenset(result.mistake_id for result in self.results if result.verdict == "pass")

    def failed_ids(self) -> frozenset[str]:
        """Candidates this report rejected."""
        return frozenset(result.mistake_id for result in self.results if result.verdict == "fail")


def report_path(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Where the confidentiality report for this run is expected."""
    return stage_dir(run_id, StageId.PRIVACY_APPROVED, root=runs_root) / REPORT_NAME


class MissingConfidentialityReportError(Exception):
    """The independent semantic verifier left no report for this run."""


def load_report(run_id: str, *, runs_root: Path | None = None) -> ConfidentialityReport:
    """Load this run's confidentiality report, or refuse.

    A missing or unreadable report is not treated as an empty one. Nothing
    downstream can distinguish "the verifier passed every record" from "the
    verifier never ran" once the attestation is stamped, so the distinction is
    enforced here, where it is still visible.
    """
    target = report_path(run_id, runs_root=runs_root)
    if not target.is_file():
        msg = (
            "this run has no confidentiality report, so the independent privacy verifier "
            f"has not run; expected it at {target.name} in the stage-7 directory. Run "
            "skills/verify-mistake-confidentiality before promoting records."
        )
        raise MissingConfidentialityReportError(msg)
    return read_model(target, ConfidentialityReport)
