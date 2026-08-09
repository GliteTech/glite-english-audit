"""The invariant the whole pipeline shape exists to provide.

Every step after ``a`` writes back the same session file names it read. That is
what lets any step's output be diffed against its input file by file, and it is
the one property no individual step can check about itself.

The failure it catches is quiet by nature. A step that skips a file it could not
process, or whose agent returns fewer files than it was given, produces a run
that looks finished and reports counts computed over less text than the person
selected. Nothing downstream notices, because every downstream step reads only
what it was handed. So the check runs between steps, on the directories, and
treats a missing file as an error rather than an absence.

An **empty** file is legal and meaningful: a session whose every message was a
duplicate, whose every word turned out to be someone else's, or that produced no
mistakes at all. Missing and empty mean different things, and only one of them is
what happened.
"""

from pathlib import Path

from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.sessions import INDEX_NAME, session_files


def compare_file_sets(previous: Path, current: Path) -> list[Diagnostic]:
    """Diagnostics for a step whose file set differs from its input's.

    ``previous`` and ``current`` are two step directories. Both directions are
    reported: a name that vanished is lost work, and a name that appeared is a
    file no input accounts for, which means some later count has a numerator
    with no denominator.
    """
    before = {path.name for path in session_files(previous)}
    after = {path.name for path in session_files(current)}

    diagnostics: list[Diagnostic] = []
    for name in sorted(before - after):
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"{current.name} is missing {name}, which {previous.name} has; "
                "a session that produced nothing is written as an empty file, not dropped",
                item_ref=name,
            )
        )
    for name in sorted(after - before):
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"{current.name} has {name}, which {previous.name} does not; "
                "no step may invent a session",
                item_ref=name,
            )
        )
    return diagnostics


def compare_line_counts(previous: Path, current: Path) -> list[Diagnostic]:
    """Diagnostics for a step that changed how many lines a file holds.

    Only steps b and c are held to this: they carry utterances, and step c must
    return every item it was given, with the text of a wholly unauthored one
    emptied rather than the item deleted. Steps d and e hold mistake records,
    where a different count is the normal case.
    """
    diagnostics: list[Diagnostic] = []
    for path in session_files(current):
        source = previous / path.name
        if not source.is_file():
            continue  # Reported by compare_file_sets; not repeated here.
        expected = _line_count(source)
        actual = _line_count(path)
        if expected != actual:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"{path.name} has {actual} items in {current.name} and {expected} in "
                    f"{previous.name}; an utterance nobody authored keeps its line with empty "
                    "text so the two files stay diffable",
                    item_ref=path.name,
                )
            )
    return diagnostics


def index_carried_forward(previous: Path, current: Path) -> list[Diagnostic]:
    """Diagnostics when a step failed to carry the session index across.

    The index is the only place a sequence number is connected to a session, so
    a step that writes files without it leaves a run nobody can trace back to
    the sessions it came from.
    """
    if not (previous / INDEX_NAME).is_file() or (current / INDEX_NAME).is_file():
        return []
    return [
        Diagnostic.from_code(
            "CARDINALITY_MISMATCH",
            f"{current.name} has no {INDEX_NAME}; the mapping from file name to session "
            "exists only here and is not recoverable from the files themselves",
        )
    ]


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())
