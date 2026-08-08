"""Schema export completeness, drift detection, and committed-schema parity."""

from pathlib import Path

from glite_english_audit.artifacts.schema_export import (
    EXPORTED_MODELS,
    export_schemas,
    schemas_dir,
)

_EXPECTED_FILES = {
    "submission_package.schema.json",
    "new_submission_request.schema.json",
    "report_lookup_request.schema.json",
    "submission_accepted.schema.json",
    "submission_rejected.schema.json",
}


def test_exported_model_names_are_the_contract_surface() -> None:
    assert {f"{name}.schema.json" for name in EXPORTED_MODELS} == _EXPECTED_FILES


def test_export_writes_all_schema_files(tmp_path: Path) -> None:
    drifted = export_schemas(tmp_path, check=False)
    assert set(drifted) == _EXPECTED_FILES
    assert {path.name for path in tmp_path.glob("*.schema.json")} == _EXPECTED_FILES
    for path in tmp_path.glob("*.schema.json"):
        assert path.read_text(encoding="utf-8").endswith("\n")


def test_check_mode_reports_all_names_on_empty_dir(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    drifted = export_schemas(target, check=True)
    assert set(drifted) == _EXPECTED_FILES
    assert list(target.glob("*.schema.json")) == []


def test_check_mode_reports_nothing_after_export(tmp_path: Path) -> None:
    export_schemas(tmp_path, check=False)
    assert export_schemas(tmp_path, check=True) == []


def test_export_is_idempotent(tmp_path: Path) -> None:
    export_schemas(tmp_path, check=False)
    assert export_schemas(tmp_path, check=False) == []


def test_committed_schemas_match_current_models() -> None:
    assert export_schemas(schemas_dir(), check=True) == []
