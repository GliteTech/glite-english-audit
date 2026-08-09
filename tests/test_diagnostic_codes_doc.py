"""The committed diagnostic-codes specification must track the registry.

``specifications/diagnostic_codes.md`` promises that "a sync test fails when
this table and the registry disagree". Checking that each code string appears
somewhere in the file does not keep that promise: the severity and description
columns can say anything, and whole families can be deleted from the registry
while the document still lists them. These tests parse the table and compare it
row for row.
"""

import re
from pathlib import Path

from glite_english_audit.diagnostics.codes import DIAGNOSTIC_DEFINITIONS, WITHHELD_REASON_CODES

_DOC = Path(__file__).resolve().parent.parent / "specifications" / "diagnostic_codes.md"
_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*([a-z]+)\s*\|\s*(.+?)\s*\|$")
_FAMILY_CELL = re.compile(r"`([A-Z][A-Z0-9_]*_)`")


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _documented_rows() -> dict[str, tuple[str, str]]:
    """Every `code | severity | description` row in the document."""
    rows: dict[str, tuple[str, str]] = {}
    for line in _doc_text().splitlines():
        match = _ROW.match(line)
        if match is not None:
            rows[match.group(1)] = (match.group(2), match.group(3))
    return rows


def test_the_code_table_matches_the_registry_row_for_row() -> None:
    documented = _documented_rows()
    registry = {
        code: (definition.severity.value, definition.description)
        for code, definition in DIAGNOSTIC_DEFINITIONS.items()
    }
    assert documented.keys() == registry.keys(), (
        f"only in the document: {sorted(documented.keys() - registry.keys())}; "
        f"only in the registry: {sorted(registry.keys() - documented.keys())}"
    )
    differing = sorted(code for code in registry if documented[code] != registry[code])
    assert not differing, f"severity or description differs for: {differing}"


def test_every_registry_family_has_a_prefix_row() -> None:
    """Adding a family without documenting it is the step the doc says to take."""
    documented_families = set(_FAMILY_CELL.findall(_doc_text()))
    registry_families = {f"{code.split('_', 1)[0]}_" for code in DIAGNOSTIC_DEFINITIONS}
    missing = sorted(registry_families - documented_families)
    assert not missing, f"families with no row in the family table: {missing}"


def test_every_withheld_reason_code_is_documented() -> None:
    text = _doc_text()
    missing = [code for code in sorted(WITHHELD_REASON_CODES) if code not in text]
    assert not missing, f"withheld codes missing from the specification: {missing}"


def test_no_documented_code_is_absent_from_the_registry() -> None:
    """Catches a code named in the prose, not only one listed in the table.

    Scoped to the diagnostic families so that backticked Python identifiers,
    such as the ``WITHHELD_REASON_CODES`` constant, are not read as codes.
    """
    families = sorted({f"{code.split('_', 1)[0]}_" for code in DIAGNOSTIC_DEFINITIONS})
    pattern = r"`((?:" + "|".join(families) + r")[A-Z0-9_]+)`"
    referenced = set(re.findall(pattern, _doc_text()))
    unknown = sorted(code for code in referenced if code not in DIAGNOSTIC_DEFINITIONS)
    assert not unknown, f"documented codes not present in the registry: {unknown}"
