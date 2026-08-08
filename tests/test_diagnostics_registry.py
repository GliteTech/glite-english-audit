"""Diagnostic registry integrity and Diagnostic construction."""

import pytest

from glite_english_audit.diagnostics.codes import (
    DIAGNOSTIC_DEFINITIONS,
    WITHHELD_REASON_CODES,
    Diagnostic,
    Severity,
    definition_for,
)


def test_every_registry_code_has_non_empty_description() -> None:
    assert DIAGNOSTIC_DEFINITIONS
    for code, definition in DIAGNOSTIC_DEFINITIONS.items():
        assert definition.code == code
        assert definition.description.strip()
        assert isinstance(definition.severity, Severity)


def test_registry_codes_are_stable_uppercase_identifiers() -> None:
    for code in DIAGNOSTIC_DEFINITIONS:
        assert code == code.upper()
        assert " " not in code


@pytest.mark.parametrize(
    ("code", "severity"),
    [
        ("STATE_INVALID_TRANSITION", Severity.ERROR),
        ("SOURCE_NOT_FOUND", Severity.INFO),
        ("SOURCE_LOCKED", Severity.WARNING),
        ("PRIVACY_URL_PRESENT", Severity.ERROR),
    ],
)
def test_from_code_takes_severity_from_registry(code: str, severity: Severity) -> None:
    diagnostic = Diagnostic.from_code(code, "synthetic test message")
    assert diagnostic.code == code
    assert diagnostic.severity is severity
    assert diagnostic.message == "synthetic test message"


def test_from_code_carries_optional_references() -> None:
    diagnostic = Diagnostic.from_code(
        "CARDINALITY_MISMATCH",
        "line count disagrees with manifest",
        item_ref="utt-0001",
        evidence_path="stage-2/candidates.jsonl",
    )
    assert diagnostic.item_ref == "utt-0001"
    assert diagnostic.evidence_path == "stage-2/candidates.jsonl"


def test_from_code_rejects_unknown_code() -> None:
    with pytest.raises(KeyError):
        Diagnostic.from_code("NOT_A_REGISTERED_CODE", "message")


def test_definition_for_rejects_unknown_code() -> None:
    with pytest.raises(KeyError):
        definition_for("NOT_A_REGISTERED_CODE")


def test_withheld_reason_codes_contents() -> None:
    expected = frozenset(
        {
            "WITHHELD_BY_USER",
            "WITHHELD_PRIVACY_UNSAFE",
            "WITHHELD_PROCESSING_FAILED",
        }
    )
    assert expected == WITHHELD_REASON_CODES
