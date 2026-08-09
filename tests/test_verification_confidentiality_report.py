"""The package must not attest to a privacy check that never ran.

Double protection is the product's central privacy claim: a deterministic
scanner and an independent semantic verifier must both clear a record before it
can be shared. Only the deterministic half was enforced. No module read the
semantic verifier's report, nothing required one to exist, and stage 8 stamped
``privacy_verifier_version`` onto every approved record regardless — so running
the scanner, skipping the confidentiality skill entirely, and building the
package produced bytes identical to a package whose records had passed both
gates.

The field Glite receives as evidence that an independent verifier checked these
records was evidence of nothing.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from confidentiality_stub import STUB_VERIFIER_VERSION, write_confidentiality_report
from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    Modality,
    OsEnvironment,
    RunStatus,
    StageId,
)
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.artifacts.models import SafeMistakeRecord, SafeRecordCandidate
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline import promote_records
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME
from glite_english_audit.verification.confidentiality_report import (
    ConfidentialityReport,
    MissingConfidentialityReportError,
    load_report,
    report_path,
)

_RUN = "run-" + "7" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _candidate(mistake_id: str) -> SafeRecordCandidate:
    return SafeRecordCandidate(
        mistake_id=mistake_id,
        record=SafeMistakeRecord(
            mistake="Used 'very' to modify a verb directly.",
            rule="In English, 'very' cannot modify a verb; use 'really' instead.",
            example="I really like this plan.",
            example_type=ExampleType.SYNTHETIC,
            source_type="claude_code",
            modality=Modality.WRITTEN,
        ),
        creator_version="1.0.0",
    )


def _seed_run(runs_root: Path) -> None:
    """A run promote_records can record stage progress into."""
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=_NOW,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.PROCESSING,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=_NOW,
            provider_transfer_confirmed_at=_NOW,
        ),
        stages=empty_stage_map(),
        fingerprint=CompatibilityFingerprint(
            adapter_versions={},
            artifact_schema_version=MANIFEST_SCHEMA_VERSION,
            tokenizer_version=TOKENIZER_VERSION,
            skill_versions={},
            prompt_versions={},
            model_ids={},
            consent_policy_version=CONSENT_POLICY_VERSION,
        ),
    )
    (runs_root / _RUN).mkdir(parents=True, exist_ok=True)
    write_model(runs_root / _RUN / RUN_MANIFEST_FILENAME, manifest)


def _seed_candidates(runs_root: Path, ids: list[str]) -> None:
    _seed_run(runs_root)
    target = ensure_private_dir(stage_dir(_RUN, StageId.SAFE_RECORDS, root=runs_root))
    write_jsonl_models(target / "candidates.jsonl", [_candidate(i) for i in ids])


def test_a_run_with_no_report_promotes_nothing(tmp_path: Path) -> None:
    """This is the attack the security pass demonstrated end to end.

    Skipping the confidentiality skill must not produce approved records.
    """
    _seed_candidates(tmp_path, ["m-1", "m-2"])
    with pytest.raises(MissingConfidentialityReportError, match="has not run"):
        promote_records.promote(_RUN, runs_root=tmp_path)


def test_a_candidate_the_report_never_names_is_withheld(tmp_path: Path) -> None:
    # A record nobody judged is not a record that passed. Partial coverage is
    # the likelier real failure than a wholly absent report: a verifier that
    # crashed halfway leaves a report naming some of the candidates.
    _seed_candidates(tmp_path, ["m-1", "m-2"])
    write_confidentiality_report(_RUN, ["m-1"], runs_root=tmp_path)
    result = promote_records.promote(_RUN, runs_root=tmp_path)
    assert result["approved"] == 1
    assert result["withheld_for_privacy"] == 1


def test_a_candidate_the_report_failed_is_withheld(tmp_path: Path) -> None:
    _seed_candidates(tmp_path, ["m-1", "m-2"])
    write_confidentiality_report(_RUN, ["m-1"], failed_ids=["m-2"], runs_root=tmp_path)
    result = promote_records.promote(_RUN, runs_root=tmp_path)
    assert result["approved"] == 1
    assert result["withheld_for_privacy"] == 1


def test_the_report_carries_the_version_that_reaches_the_shared_record(tmp_path: Path) -> None:
    # Stage 8 stamps this onto every shared record. Taking it from the report
    # rather than from the client is what makes it an attestation about the
    # verifier that ran instead of about whatever built the package.
    _seed_candidates(tmp_path, ["m-1"])
    write_confidentiality_report(_RUN, ["m-1"], runs_root=tmp_path)
    assert load_report(_RUN, runs_root=tmp_path).verifier_version == STUB_VERIFIER_VERSION


def test_a_report_whose_counts_disagree_with_its_results_is_refused(tmp_path: Path) -> None:
    # The counts are the verifier's own summary of what it did. If they do not
    # match the verdicts, one of the two is wrong and neither can be trusted to
    # decide what leaves the machine.
    write_confidentiality_report(_RUN, ["m-1", "m-2"], runs_root=tmp_path)
    target = report_path(_RUN, runs_root=tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["counts"]["passed"] = 5
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="do not match"):
        load_report(_RUN, runs_root=tmp_path)


def test_a_report_naming_a_candidate_twice_is_refused(tmp_path: Path) -> None:
    write_confidentiality_report(_RUN, ["m-1"], runs_root=tmp_path)
    target = report_path(_RUN, runs_root=tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["results"].append({"mistake_id": "m-1", "verdict": "pass"})
    payload["counts"] = {"checked": 2, "passed": 2, "failed": 0}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="more than once"):
        load_report(_RUN, runs_root=tmp_path)


def test_a_failure_must_say_why() -> None:
    with pytest.raises(ValidationError, match="at least one diagnostic"):
        ConfidentialityReport.model_validate(
            {
                "report_type": "confidentiality_verification",
                "report_version": 1,
                "results": [{"mistake_id": "m-1", "verdict": "fail"}],
                "counts": {"checked": 1, "passed": 0, "failed": 1},
            }
        )


def test_a_pass_carrying_diagnostics_is_refused() -> None:
    # A verdict that contradicts its own evidence is not a verdict.
    with pytest.raises(ValidationError, match="no diagnostics"):
        ConfidentialityReport.model_validate(
            {
                "report_type": "confidentiality_verification",
                "report_version": 1,
                "results": [
                    {
                        "mistake_id": "m-1",
                        "verdict": "pass",
                        "diagnostics": [
                            {"code": "PRIVACY_NAME_PRESENT", "field": "example", "note": "a name"}
                        ],
                    }
                ],
                "counts": {"checked": 1, "passed": 1, "failed": 0},
            }
        )


def test_the_verifier_version_must_be_a_plain_version() -> None:
    # It leaves the machine inside the package, where the submission gate
    # rejects anything else. Catching it here beats failing the whole run at
    # the last step.
    with pytest.raises(ValidationError, match="plain dotted version"):
        ConfidentialityReport.model_validate(
            {
                "report_type": "confidentiality_verification",
                "report_version": 1,
                "results": [],
                "counts": {"checked": 0, "passed": 0, "failed": 0},
                "verifier_version": "/Users/alice/work  raw: we ship Tuesday",
            }
        )
