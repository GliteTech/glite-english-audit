"""Atomic IO behavior: round-trips, permissions, and JSONL handling."""

import os
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.io import (
    atomic_write_text,
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import EvidenceSpan, SafeMistakeRecord

_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics only")


def _safe_record() -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake="Wrote 'have went' instead of 'went'.",
        rule="Use the simple past for a finished action.",
        example="Yesterday I went to the store.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def test_write_model_read_model_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    record = _safe_record()
    write_model(path, record)
    assert read_model(path, SafeMistakeRecord) == record


def test_write_model_output_ends_with_newline(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    write_model(path, _safe_record())
    assert path.read_bytes().endswith(b"\n")


def test_atomic_write_replaces_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    atomic_write_text(path, "first")
    atomic_write_text(path, "second")
    assert path.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.iterdir()) == [path]


def test_jsonl_round_trip_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "spans.jsonl"
    spans = [EvidenceSpan(start=index, end=index + 1) for index in range(5)]
    written = write_jsonl_models(path, spans)
    assert written == 5
    assert list(read_jsonl_models(path, EvidenceSpan)) == spans


def test_write_jsonl_empty_iterable_writes_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    assert write_jsonl_models(path, []) == 0
    assert path.read_bytes() == b""


def test_read_jsonl_models_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "sparse.jsonl"
    content = '{"start": 0, "end": 1}\n\n   \n{"start": 1, "end": 2}\n\t\n'
    path.write_text(content, encoding="utf-8")
    assert list(read_jsonl_models(path, EvidenceSpan)) == [
        EvidenceSpan(start=0, end=1),
        EvidenceSpan(start=1, end=2),
    ]


@_POSIX_ONLY
def test_atomic_write_sets_owner_only_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    atomic_write_text(path, "{}")
    assert path.stat().st_mode & 0o777 == 0o600


@_POSIX_ONLY
def test_write_model_sets_owner_only_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    write_model(path, _safe_record())
    assert path.stat().st_mode & 0o777 == 0o600


@_POSIX_ONLY
def test_ensure_private_dir_sets_owner_only_mode_on_created_dirs(tmp_path: Path) -> None:
    target = tmp_path / "outer" / "inner"
    result = ensure_private_dir(target)
    assert result == target
    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "outer").stat().st_mode & 0o777 == 0o700


@_POSIX_ONLY
def test_ensure_private_dir_leaves_existing_parents_alone(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    before = existing.stat().st_mode & 0o777
    ensure_private_dir(existing / "child")
    assert existing.stat().st_mode & 0o777 == before
    assert (existing / "child").stat().st_mode & 0o777 == 0o700
