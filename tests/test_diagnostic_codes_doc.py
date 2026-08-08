"""The committed diagnostic-codes specification must track the registry."""

from pathlib import Path

from glite_english_audit.diagnostics.codes import DIAGNOSTIC_DEFINITIONS, WITHHELD_REASON_CODES

_DOC = Path(__file__).resolve().parent.parent / "specifications" / "diagnostic_codes.md"


def test_every_registry_code_is_documented() -> None:
    text = _DOC.read_text(encoding="utf-8")
    missing = [code for code in DIAGNOSTIC_DEFINITIONS if code not in text]
    assert not missing, f"codes missing from specifications/diagnostic_codes.md: {missing}"


def test_every_withheld_reason_code_is_documented() -> None:
    text = _DOC.read_text(encoding="utf-8")
    missing = [code for code in sorted(WITHHELD_REASON_CODES) if code not in text]
    assert not missing, f"withheld codes missing from the specification: {missing}"


def test_documented_codes_exist_in_registry() -> None:
    # Any ALL_CAPS token that looks like one of our code families must exist in
    # the registry, so the document cannot drift ahead of the code.
    import re

    text = _DOC.read_text(encoding="utf-8")
    families = ("SCHEMA_", "CARDINALITY_", "ARITHMETIC_", "LINEAGE_", "PRIVACY_", "SUBMISSION_")
    referenced = set(re.findall(r"\b(?:" + "|".join(families) + r")[A-Z_]+\b", text))
    known = set(DIAGNOSTIC_DEFINITIONS) | WITHHELD_REASON_CODES
    unknown = sorted(code for code in referenced if code not in known)
    assert not unknown, f"documented codes not present in the registry: {unknown}"
