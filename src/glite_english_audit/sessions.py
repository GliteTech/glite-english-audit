"""One session is one file, and the file keeps its name through every step.

This is the property the pipeline is built around: after step a, each step
reads the previous step's files and writes the same names back, so any step's
output can be diffed against its input file by file. The nine-stage layout it
replaced pooled every session into one JSONL, which made "what did this step do
to session X" unanswerable.

Filenames are opaque sequence numbers — ``session-0001.jsonl`` — with the
mapping to real session identity kept in a private index beside them. Two
reasons, both learned the hard way in this project:

- ``session_hash`` is not safe as a path component. It has no validator, and
  two adapters populate it from a JSON value read off disk without checking its
  shape. Joining it into a filename is exactly the defect fixed in commit
  ``03ff4e4``, where an unvalidated ``instance_key`` was joined into the
  snapshot path and could escape the run directory.
- It would leak into a model's context. The batch projection deliberately
  strips session hashes because "sending them into a model's context spends
  privacy for nothing", and a filename is handed to the skill and echoed back
  in its report. A hash in the path reintroduces exactly what the projection
  removes.

The index never leaves the machine and is never passed to a model.
"""

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from glite_english_audit.artifacts.io import ensure_private_dir, read_jsonl_models
from glite_english_audit.artifacts.models import NormalizedUtterance

INDEX_NAME = "session-index.json"
SESSION_GLOB = "session-*.jsonl"

_SESSION_FILE = re.compile(r"^session-(\d{4,})\.jsonl$")


def session_file_name(sequence: int) -> str:
    """The on-disk name for one session file, one-based."""
    if sequence < 1:
        msg = f"session sequence numbers start at 1, not {sequence}"
        raise ValueError(msg)
    return f"session-{sequence:04d}.jsonl"


def session_files(directory: Path) -> list[Path]:
    """Every session file in a step directory, in sequence order.

    Sorted by the number rather than the string, so a run with more than 9,999
    sessions does not silently reorder at the rollover.
    """
    found: list[tuple[int, Path]] = []
    for path in directory.glob(SESSION_GLOB):
        match = _SESSION_FILE.match(path.name)
        if match is not None:
            found.append((int(match.group(1)), path))
    return [path for _, path in sorted(found)]


def read_session(path: Path) -> list[NormalizedUtterance]:
    """Read one session file."""
    return list(read_jsonl_models(path, NormalizedUtterance))


def read_all(directory: Path) -> Iterator[tuple[Path, list[NormalizedUtterance]]]:
    """Every session in a step directory, paired with its path."""
    for path in session_files(directory):
        yield path, read_session(path)


def write_index(directory: Path, mapping: dict[str, str]) -> Path:
    """Record which sequence number belongs to which session.

    ``mapping`` is file name to session hash. It stays local: it is the one
    place the two are connected, which is what lets every later step work with
    opaque names.
    """
    ensure_private_dir(directory)
    target = directory / INDEX_NAME
    target.write_text(
        json.dumps({"sessions": mapping}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def read_index(directory: Path) -> dict[str, str]:
    """The sequence-to-session mapping, or an empty mapping if absent."""
    target = directory / INDEX_NAME
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {str(k): str(v) for k, v in sessions.items()}


def group_by_session(
    utterances: Iterable[NormalizedUtterance],
) -> list[tuple[str, list[NormalizedUtterance]]]:
    """Split a flat list into sessions, deterministically ordered.

    Sessions are ordered by their earliest utterance, so sequence numbers track
    the order the person actually worked in. Within a session, utterances keep
    chronological order with undated ones last. Both orderings are stable
    against input order, so two runs over the same data produce the same
    sequence numbers and therefore the same filenames.
    """
    grouped: dict[str, list[NormalizedUtterance]] = {}
    for utterance in utterances:
        grouped.setdefault(utterance.session_hash, []).append(utterance)

    def _within(utterance: NormalizedUtterance) -> tuple[int, float, str]:
        if utterance.timestamp is None:
            return (1, 0.0, utterance.utterance_id)
        return (0, utterance.timestamp.timestamp(), utterance.utterance_id)

    ordered: list[tuple[tuple[int, float, str], str, list[NormalizedUtterance]]] = []
    for session_hash, members in grouped.items():
        members.sort(key=_within)
        ordered.append((_within(members[0]), session_hash, members))
    ordered.sort()
    return [(session_hash, members) for _, session_hash, members in ordered]
