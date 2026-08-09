"""Tests for the deterministic stage-4 findings-format validator."""

from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope
from glite_english_audit.artifacts.hashing import sha256_hex
from glite_english_audit.artifacts.models import FindingsArtifactMeta
from glite_english_audit.verification.findings_format import (
    EMPTY_RESULT_LINE,
    THRESHOLD_LINE,
    TITLE_LINE,
    validate_findings_body,
    verify_findings_artifact,
)

_HEADER = f"{TITLE_LINE}\n\n{THRESHOLD_LINE}\n\n"

VALID_ONE_FINDING = (
    _HEADER
    + "## Finding 1\n"
    + "\n"
    + "Original: I very like this approach.\n"
    + "Correction: I really like this approach.\n"
    + 'Why: "Very" cannot modify a verb directly.\n'
)

VALID_TWO_FINDINGS = (
    VALID_ONE_FINDING
    + "\n"
    + "## Finding 2\n"
    + "\n"
    + "Original: He suggested me to wait.\n"
    + "Correction: He suggested that I wait.\n"
    + "Why: Suggest does not take an indirect object with an infinitive.\n"
    + "Uncertainty: The sentence could also be rephrased with a gerund.\n"
)

VALID_EMPTY = _HEADER + EMPTY_RESULT_LINE + "\n"


def test_valid_single_finding() -> None:
    parsed, diagnostics = validate_findings_body(VALID_ONE_FINDING, item_ref="u1")
    assert diagnostics == []
    assert parsed is not None
    assert parsed.finding_count == 1
    assert parsed.no_mistakes_found is False


def test_valid_two_findings_with_uncertainty() -> None:
    parsed, diagnostics = validate_findings_body(VALID_TWO_FINDINGS, item_ref="u1")
    assert diagnostics == []
    assert parsed is not None
    assert parsed.finding_count == 2


def test_valid_empty_result() -> None:
    parsed, diagnostics = validate_findings_body(VALID_EMPTY, item_ref="u1")
    assert diagnostics == []
    assert parsed is not None
    assert parsed.no_mistakes_found is True
    assert parsed.finding_count == 0


def test_rejects_wrong_title() -> None:
    parsed, diagnostics = validate_findings_body(
        VALID_ONE_FINDING.replace(TITLE_LINE, "# Findings"), item_ref="u1"
    )
    assert parsed is None
    assert diagnostics[0].code == "SCHEMA_INVALID_VALUE"


def test_rejects_modified_threshold() -> None:
    parsed, _ = validate_findings_body(
        VALID_ONE_FINDING.replace("strongly", "sort of"), item_ref="u1"
    )
    assert parsed is None


def test_rejects_numbering_gap() -> None:
    parsed, _ = validate_findings_body(
        VALID_ONE_FINDING.replace("## Finding 1", "## Finding 2"), item_ref="u1"
    )
    assert parsed is None


def test_rejects_missing_correction_line() -> None:
    broken = VALID_ONE_FINDING.replace("Correction: I really like this approach.\n", "")
    parsed, _ = validate_findings_body(broken, item_ref="u1")
    assert parsed is None


def test_rejects_missing_trailing_newline() -> None:
    parsed, _ = validate_findings_body(VALID_ONE_FINDING.rstrip("\n"), item_ref="u1")
    assert parsed is None


def test_rejects_stray_text_after_empty_sentence() -> None:
    parsed, _ = validate_findings_body(VALID_EMPTY + "Extra.\n", item_ref="u1")
    assert parsed is None


def _meta(body: str, *, finding_count: int, empty: bool) -> FindingsArtifactMeta:
    return FindingsArtifactMeta(
        envelope=ArtifactEnvelope(
            schema_name="plain_findings",
            schema_version=1,
            artifact_id="art-" + "a" * 32,
            run_id="run-" + "b" * 32,
            stage_id=StepId.D_MISTAKES,
            producer_name="analyze-english-text",
            producer_version="1.0.0",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        ),
        unit_id="unit-1",
        utterance_ids=["claude_code-abc-1"],
        finding_count=finding_count,
        no_mistakes_found=empty,
        body_relative_path="unit-1.md",
        body_sha256=sha256_hex(body.encode("utf-8")),
    )


def test_verify_findings_artifact_round_trip(tmp_path: Path) -> None:
    body_path = tmp_path / "unit-1.md"
    body_path.write_text(VALID_ONE_FINDING, encoding="utf-8")
    meta = _meta(VALID_ONE_FINDING, finding_count=1, empty=False)
    assert verify_findings_artifact(body_path, meta, item_ref="unit-1") == []


def test_verify_findings_artifact_detects_hash_mismatch(tmp_path: Path) -> None:
    body_path = tmp_path / "unit-1.md"
    body_path.write_text(VALID_ONE_FINDING, encoding="utf-8")
    meta = _meta(VALID_TWO_FINDINGS, finding_count=1, empty=False)
    codes = {d.code for d in verify_findings_artifact(body_path, meta, item_ref="unit-1")}
    assert "LINEAGE_HASH_MISMATCH" in codes


def test_verify_findings_artifact_detects_count_mismatch(tmp_path: Path) -> None:
    body_path = tmp_path / "unit-1.md"
    body_path.write_text(VALID_TWO_FINDINGS, encoding="utf-8")
    meta = _meta(VALID_TWO_FINDINGS, finding_count=1, empty=False)
    codes = {d.code for d in verify_findings_artifact(body_path, meta, item_ref="unit-1")}
    assert "CARDINALITY_MISMATCH" in codes


def test_verify_findings_artifact_missing_body(tmp_path: Path) -> None:
    meta = _meta(VALID_ONE_FINDING, finding_count=1, empty=False)
    codes = {
        d.code for d in verify_findings_artifact(tmp_path / "missing.md", meta, item_ref="unit-1")
    }
    assert codes == {"LINEAGE_MISSING_INPUT"}
