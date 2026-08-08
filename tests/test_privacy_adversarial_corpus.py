"""Adversarial privacy corpus: committed expectations match the scanner exactly.

The corpus in ``fixtures/privacy_adversarial`` carries two kinds of cases:
deterministic ones, whose ``expect_deterministic`` codes the pattern scanner
must actually report, and semantic re-identification ones, which must pass the
scanner untouched because only the semantic confidentiality verifier can catch
them. Clean negatives pin the scanner's false-positive boundary.
"""

import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from glite_english_audit.artifacts.models import SafeMistakeRecord
from glite_english_audit.verification.privacy_scanner import scan_safe_record

_CASES_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "privacy_adversarial" / "cases.jsonl"
)

_MINIMUM_TOTAL = 520
_CATEGORY_MINIMUMS: dict[str, int] = {
    "direct_secret": 60,
    "contact_identifier": 60,
    "path_code": 60,
    "quantity": 50,
    "context_rule": 40,
    "long_phrase": 30,
    "semantic_reidentification": 120,
    "clean_negative": 100,
}

# Mirrors the scanner's credential pattern: every match in the corpus must be
# unmistakably fake (fixture policy: FAKE or EXAMPLE in the secret body).
_CREDENTIAL_SHAPE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{8,}"
    r"|\bxox[a-z]-[A-Za-z0-9-]+"
    r"|\bAKIA[A-Z0-9]{8,}"
    r"|\bAIza[A-Za-z0-9_-]{8,}"
    r"|BEGIN [A-Z ]*PRIVATE KEY"
    r"|\beyJ[A-Za-z0-9_-]{10,}"
)


class CorpusCase(BaseModel):
    """One line of cases.jsonl."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    semantic_only: bool
    expect_deterministic: tuple[str, ...]
    record: SafeMistakeRecord


def _load_cases() -> list[CorpusCase]:
    lines = _CASES_PATH.read_text(encoding="utf-8").splitlines()
    return [CorpusCase.model_validate_json(line) for line in lines if line.strip()]


_CASES = _load_cases()
_ADVERSARIAL = [
    case for case in _CASES if not case.semantic_only and case.category != "clean_negative"
]
_CLEAN = [case for case in _CASES if case.category == "clean_negative"]
_SEMANTIC = [case for case in _CASES if case.semantic_only]


def _actual_codes(record: SafeMistakeRecord) -> set[str]:
    return {diagnostic.code for diagnostic in scan_safe_record(record)}


def test_corpus_meets_total_and_category_minimums() -> None:
    assert len(_CASES) >= _MINIMUM_TOTAL
    for category, minimum in _CATEGORY_MINIMUMS.items():
        count = sum(1 for case in _CASES if case.category == category)
        assert count >= minimum, f"{category}: {count} < {minimum}"


def test_every_line_is_a_valid_case() -> None:
    non_empty = [
        line for line in _CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(_CASES) == len(non_empty)


def test_case_ids_are_unique() -> None:
    case_ids = [case.case_id for case in _CASES]
    assert len(case_ids) == len(set(case_ids))


def test_semantic_only_flag_matches_category() -> None:
    for case in _CASES:
        assert case.semantic_only == (case.category == "semantic_reidentification")
        if case.semantic_only:
            assert case.expect_deterministic == ()


@pytest.mark.parametrize("case", _ADVERSARIAL, ids=[case.case_id for case in _ADVERSARIAL])
def test_adversarial_expectations_fire(case: CorpusCase) -> None:
    assert case.expect_deterministic, "a deterministic adversarial case must expect codes"
    actual = _actual_codes(case.record)
    missing = set(case.expect_deterministic) - actual
    assert not missing, f"expected codes the scanner did not report: {sorted(missing)}"


@pytest.mark.parametrize("case", _CLEAN, ids=[case.case_id for case in _CLEAN])
def test_clean_negative_is_not_flagged(case: CorpusCase) -> None:
    assert case.expect_deterministic == ()
    assert scan_safe_record(case.record) == []


@pytest.mark.parametrize("case", _SEMANTIC, ids=[case.case_id for case in _SEMANTIC])
def test_semantic_case_passes_the_deterministic_scanner(case: CorpusCase) -> None:
    assert scan_safe_record(case.record) == [], (
        "semantic re-identification cases belong to the semantic verifier and must "
        "pass every deterministic pattern"
    )


def test_every_credential_shaped_string_is_unmistakably_fake() -> None:
    for case in _CASES:
        for field_name in ("mistake", "rule", "example"):
            value = getattr(case.record, field_name)
            for match in _CREDENTIAL_SHAPE.findall(value):
                assert "FAKE" in match or "EXAMPLE" in match, (
                    f"{case.case_id}.{field_name}: credential-shaped string without a "
                    f"FAKE or EXAMPLE marker"
                )
