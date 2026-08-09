"""The file-set invariant, tested directly rather than inferred from the steps.

Every step after a writes back the file names it read. The pipeline was reshaped
to provide that property, so it is checked here on its own terms: what happens
when a step drops a file, invents one, changes an item count, or loses the index.
"""

from pathlib import Path

from glite_english_audit.sessions import INDEX_NAME
from glite_english_audit.verification.step_layout import (
    compare_file_sets,
    compare_line_counts,
    index_carried_forward,
)


def _step(root: Path, name: str, files: dict[str, int]) -> Path:
    """A step directory holding ``files`` mapped to a line count."""
    directory = root / name
    directory.mkdir()
    for file_name, lines in files.items():
        body = "".join(f'{{"n": {index}}}\n' for index in range(lines))
        (directory / file_name).write_text(body, encoding="utf-8")
    return directory


def test_matching_file_sets_produce_nothing(tmp_path: Path) -> None:
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 3, "session-0002.jsonl": 1})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 3, "session-0002.jsonl": 1})
    assert compare_file_sets(before, after) == []
    assert compare_line_counts(before, after) == []


def test_a_dropped_file_is_an_error(tmp_path: Path) -> None:
    # The defect this module exists for: a step that skips a file it could not
    # process leaves a run that looks finished and counts less text than the
    # person selected.
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 3, "session-0002.jsonl": 1})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 3})
    found = compare_file_sets(before, after)
    assert [d.code for d in found] == ["CARDINALITY_MISMATCH"]
    assert found[0].item_ref == "session-0002.jsonl"
    assert found[0].severity.value == "error"


def test_an_emptied_file_is_not_a_dropped_file(tmp_path: Path) -> None:
    # A session whose every word turned out to be someone else's is an empty
    # file, and that is a legal outcome, not a missing one.
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 2})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 0})
    assert compare_file_sets(before, after) == []


def test_an_invented_file_is_an_error(tmp_path: Path) -> None:
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 1})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 1, "session-0009.jsonl": 4})
    found = compare_file_sets(before, after)
    assert [d.item_ref for d in found] == ["session-0009.jsonl"]


def test_a_changed_item_count_is_an_error(tmp_path: Path) -> None:
    # Step c must return every item it was given. An utterance that was entirely
    # someone else's text comes back with empty text, not deleted.
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 5})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 4})
    found = compare_line_counts(before, after)
    assert len(found) == 1
    assert "4 items" in found[0].message
    assert "5" in found[0].message


def test_a_missing_file_is_reported_once_not_twice(tmp_path: Path) -> None:
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 5})
    after = _step(tmp_path, "c-authored", {})
    assert len(compare_file_sets(before, after)) == 1
    assert compare_line_counts(before, after) == []


def test_a_lost_index_is_an_error(tmp_path: Path) -> None:
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 1})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 1})
    (before / INDEX_NAME).write_text('{"sessions": {}}\n', encoding="utf-8")
    found = index_carried_forward(before, after)
    assert [d.code for d in found] == ["CARDINALITY_MISMATCH"]

    (after / INDEX_NAME).write_text('{"sessions": {}}\n', encoding="utf-8")
    assert index_carried_forward(before, after) == []


def test_the_index_is_not_counted_as_a_session_file(tmp_path: Path) -> None:
    before = _step(tmp_path, "b-deduplicated", {"session-0001.jsonl": 1})
    after = _step(tmp_path, "c-authored", {"session-0001.jsonl": 1})
    (after / INDEX_NAME).write_text('{"sessions": {}}\n', encoding="utf-8")
    (after / "removed.json").write_text("{}", encoding="utf-8")
    assert compare_file_sets(before, after) == []
