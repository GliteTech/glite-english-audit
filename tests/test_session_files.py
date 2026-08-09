"""One session is one file, and the file keeps its name through every step.

This is the property the whole pipeline shape exists to provide, so it is
tested directly rather than inferred from the steps that rely on it. The layout
it replaced pooled every session into one JSONL and could not answer "what did
this step do to session X" at all.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import Modality, TextStatus
from glite_english_audit.artifacts.io import write_jsonl_models
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.sessions import (
    INDEX_NAME,
    group_by_session,
    read_index,
    read_session,
    session_file_name,
    session_files,
    write_index,
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _utterance(index: int, session: str, *, minute: int = 0) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"claude_code-0123456789abcdef-{index:04d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash=session,
        timestamp=_NOW.replace(minute=minute),
        text=f"message {index}",
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="c" * 64,
    )


def test_a_filename_carries_no_session_identity() -> None:
    """The filename is an opaque sequence number, deliberately.

    session_hash has no validator and two adapters populate it from a JSON
    value read off disk, so joining it into a path repeats the defect fixed in
    commit 03ff4e4. It would also leak session identity into a model's context,
    since the filename is handed to the skill and echoed back in its report —
    exactly what the batch projection strips out.
    """
    name = session_file_name(7)
    assert name == "session-0007.jsonl"
    assert "a" * 64 not in name


def test_sequence_numbers_start_at_one() -> None:
    with pytest.raises(ValueError, match="start at 1"):
        session_file_name(0)


def test_sessions_are_ordered_by_when_they_started(tmp_path: Path) -> None:
    # Sequence numbers should track the order the person worked in, so the
    # same data always produces the same filenames.
    late = _utterance(1, "b" * 64, minute=30)
    early = _utterance(2, "a" * 64, minute=5)
    grouped = group_by_session([late, early])
    assert [session for session, _ in grouped] == ["a" * 64, "b" * 64]


def test_grouping_does_not_depend_on_input_order(tmp_path: Path) -> None:
    members = [
        _utterance(1, "a" * 64, minute=10),
        _utterance(2, "b" * 64, minute=5),
        _utterance(3, "a" * 64, minute=1),
    ]
    forward = group_by_session(members)
    backward = group_by_session(list(reversed(members)))
    assert [s for s, _ in forward] == [s for s, _ in backward]
    assert [[u.utterance_id for u in m] for _, m in forward] == [
        [u.utterance_id for u in m] for _, m in backward
    ]


def test_files_are_listed_by_number_not_by_string(tmp_path: Path) -> None:
    # At the four-digit rollover a string sort puts session-10000 before
    # session-9999, which would silently reorder a large run.
    for sequence in (9, 10, 9999, 10000):
        (tmp_path / f"session-{sequence:04d}.jsonl").write_text("", encoding="utf-8")
    assert [p.name for p in session_files(tmp_path)] == [
        "session-0009.jsonl",
        "session-0010.jsonl",
        "session-9999.jsonl",
        "session-10000.jsonl",
    ]


def test_a_stray_file_is_not_mistaken_for_a_session(tmp_path: Path) -> None:
    (tmp_path / "session-0001.jsonl").write_text("", encoding="utf-8")
    (tmp_path / INDEX_NAME).write_text("{}", encoding="utf-8")
    (tmp_path / "removed.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.jsonl").write_text("", encoding="utf-8")
    assert [p.name for p in session_files(tmp_path)] == ["session-0001.jsonl"]


def test_the_index_round_trips(tmp_path: Path) -> None:
    mapping = {"session-0001.jsonl": "a" * 64, "session-0002.jsonl": "b" * 64}
    write_index(tmp_path, mapping)
    assert read_index(tmp_path) == mapping


def test_a_missing_or_corrupt_index_reads_as_empty(tmp_path: Path) -> None:
    # The index is a convenience for a human reading a run; losing it must not
    # stop a step that only needs the files.
    assert read_index(tmp_path) == {}
    (tmp_path / INDEX_NAME).write_text("{not json", encoding="utf-8")
    assert read_index(tmp_path) == {}


def test_a_session_file_round_trips(tmp_path: Path) -> None:
    members = [_utterance(1, "a" * 64), _utterance(2, "a" * 64, minute=1)]
    path = tmp_path / session_file_name(1)
    write_jsonl_models(path, members)
    assert [u.utterance_id for u in read_session(path)] == [u.utterance_id for u in members]


def test_the_index_holds_identity_and_the_filenames_do_not(tmp_path: Path) -> None:
    # The one place sequence and session are connected. It stays local and is
    # never passed to a model.
    write_index(tmp_path, {"session-0001.jsonl": "a" * 64})
    blob = (tmp_path / INDEX_NAME).read_text(encoding="utf-8")
    assert "a" * 64 in blob
    assert set(json.loads(blob)) == {"sessions"}
