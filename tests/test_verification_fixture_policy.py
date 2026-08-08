"""Fixture framework policy: declarations, validation, and literal enforcement."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.verification.fixture_policy import (
    FixtureMeta,
    check_fixture_tree,
    load_fixture_meta,
)


def _codes(diagnostics: list[Diagnostic]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _write_meta(directory: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "adapter_id": "claude_code",
        "kind": "success",
        "description": "Synthetic sample data for tests.",
        "synthetic": True,
        "storage_variant": None,
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    meta_path = directory / "fixture.json"
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    return meta_path


def test_declared_fixture_passes(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures" / "claude_code" / "success"
    _write_meta(fixture_dir)
    (fixture_dir / "sample.jsonl").write_text('{"text": "synthetic"}\n', encoding="utf-8")
    nested = fixture_dir / "nested"
    nested.mkdir()
    (nested / "more.jsonl").write_text('{"text": "synthetic"}\n', encoding="utf-8")
    assert check_fixture_tree(tmp_path) == []


def test_missing_fixtures_directory_is_clean(tmp_path: Path) -> None:
    assert check_fixture_tree(tmp_path) == []


def test_file_without_declaration_flagged(tmp_path: Path) -> None:
    orphan_dir = tmp_path / "fixtures" / "claude_code" / "orphan"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "data.jsonl").write_text('{"text": "synthetic"}\n', encoding="utf-8")
    diagnostics = check_fixture_tree(tmp_path)
    assert _codes(diagnostics) == ["SCHEMA_MISSING_FIELD"]
    assert diagnostics[0].item_ref is not None
    assert "orphan/data.jsonl" in diagnostics[0].item_ref


def test_invalid_fixture_json_flagged(tmp_path: Path) -> None:
    broken_dir = tmp_path / "fixtures" / "claude_code" / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "fixture.json").write_text("not json at all {", encoding="utf-8")
    diagnostics = check_fixture_tree(tmp_path)
    assert _codes(diagnostics) == ["SCHEMA_INVALID_VALUE"]


def test_unknown_kind_flagged_in_tree(tmp_path: Path) -> None:
    _write_meta(tmp_path / "fixtures" / "claude_code" / "bad-kind", kind="production")
    diagnostics = check_fixture_tree(tmp_path)
    assert _codes(diagnostics) == ["SCHEMA_INVALID_VALUE"]


def test_non_synthetic_declaration_flagged_in_tree(tmp_path: Path) -> None:
    _write_meta(tmp_path / "fixtures" / "claude_code" / "claims-real", synthetic=False)
    diagnostics = check_fixture_tree(tmp_path)
    assert _codes(diagnostics) == ["SCHEMA_INVALID_VALUE"]


def test_load_fixture_meta_round_trip(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures" / "codex" / "empty"
    _write_meta(fixture_dir, adapter_id="codex", kind="empty")
    meta = load_fixture_meta(fixture_dir)
    assert meta.adapter_id == "codex"
    assert meta.kind == "empty"
    assert meta.synthetic is True


@pytest.mark.parametrize(
    "kind",
    ["success", "empty", "malformed", "unsupported", "migration", "unit"],
)
def test_fixture_meta_accepts_every_declared_kind(kind: str) -> None:
    meta = FixtureMeta.model_validate(
        {
            "adapter_id": "claude_code",
            "kind": kind,
            "description": "Synthetic sample data for tests.",
            "synthetic": True,
        }
    )
    assert meta.kind == kind


def test_fixture_meta_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        FixtureMeta.model_validate(
            {
                "adapter_id": "claude_code",
                "kind": "real-user-data",
                "description": "Never allowed.",
                "synthetic": True,
            }
        )


def test_fixture_meta_requires_synthetic_true() -> None:
    with pytest.raises(ValidationError):
        FixtureMeta.model_validate(
            {
                "adapter_id": "claude_code",
                "kind": "success",
                "description": "Claims to be real data.",
                "synthetic": False,
            }
        )


def test_fixture_meta_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        FixtureMeta.model_validate(
            {
                "adapter_id": "claude_code",
                "kind": "success",
                "description": "Synthetic sample data for tests.",
                "synthetic": True,
                "source_path": "/tmp/example",
            }
        )
