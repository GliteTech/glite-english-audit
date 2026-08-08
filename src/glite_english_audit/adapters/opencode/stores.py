"""OpenCode storage-generation parsing and cross-generation deduplication.

Implements the record schemas, inclusion rules, and dedup rules from
``specifications/sources/opencode.md`` (sections 3, 5, and 7) for the three
storage generations: the current WAL SQLite database (S), the flat global
JSON tree (J2), and the per-project JSON tree (J1). Every parse is
feature-detected per record; nothing branches on a version string. All file
and database access goes through caller-supplied callbacks so the adapter
keeps a single audited allowlist gate.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENERATION_SQLITE = "sqlite"
GENERATION_J2 = "json-j2"
GENERATION_J1 = "json-j1"
GENERATION_ORDER: tuple[str, ...] = (GENERATION_SQLITE, GENERATION_J2, GENERATION_J1)
_GENERATION_PRIORITY: dict[str, int] = {GENERATION_SQLITE: 3, GENERATION_J2: 2, GENERATION_J1: 1}

TEXT_PART_TYPE = "text"

# Spec 3.1/3.4: probe before selecting; a database missing any of these is
# unsupported, never guessed about.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "session": frozenset({"id", "project_id", "parent_id", "time_created"}),
    "message": frozenset({"id", "session_id", "time_created", "data"}),
    "part": frozenset({"id", "message_id", "session_id", "data"}),
}

# Spec 10.1: sessions present while the projected v1 tables are empty and the
# event-sourced v2 layer exists means projection stopped; fail closed.
V2_TABLE_NAMES: frozenset[str] = frozenset({"session_message", "session_input"})

# The only tables a sanitized snapshot may keep (spec 2.3, 8).
SNAPSHOT_KEEP_TABLES: frozenset[str] = frozenset({"session", "message", "part"})

# Spec 10.2: vendor slash-command template bodies persisted as ordinary user
# text parts. Exact-match denylist, fixture-tested; precision loss accepted.
VENDOR_TEMPLATE_TEXTS: frozenset[str] = frozenset(
    {
        "Please analyze this codebase and create an AGENTS.md file containing:\n"
        "1. Build/lint/test commands - especially for running a single test\n"
        "2. Code style guidelines including imports, formatting, types, naming conventions, etc."
    }
)


def ms_to_datetime(value: object) -> datetime | None:
    """Unix-epoch-milliseconds to aware UTC datetime; anything else is None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionCopy:
    """One generation's copy of a session record."""

    generation: str
    store_order: int
    project_key: str
    session_id: str
    parent_id: str | None
    version: str | None
    created: datetime | None


@dataclass(frozen=True)
class MessageCopy:
    """One generation's copy of a message record, with its candidate text."""

    generation: str
    store_order: int
    project_key: str | None
    session_id: str
    message_id: str
    role: str | None
    timestamp: datetime | None
    candidate_text: str | None
    candidate_part_ids: tuple[str, ...]
    part_count: int


@dataclass
class StoreScan:
    """Aggregate parse result for one store (one database or one JSON tree)."""

    generation: str
    order: int
    store_path: Path
    supported: bool = True
    diagnostic_code: str | None = None
    sessions: list[SessionCopy] = field(default_factory=list)
    messages: list[MessageCopy] = field(default_factory=list)
    file_count: int = 0
    malformed: int = 0
    unrecognized: int = 0


@dataclass(frozen=True)
class Candidate:
    """One canonical candidate utterance after cross-generation dedup."""

    project_key: str
    session_id: str
    message_id: str
    text: str
    part_ids: tuple[str, ...]
    timestamp: datetime | None
    generation_text_mismatch: bool
    session_version: str | None


_SORT_SENTINEL = datetime.max.replace(tzinfo=UTC)

Reader = Callable[[Path], str]
Connector = Callable[[Path], sqlite3.Connection]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _created_from(obj: dict[str, Any]) -> datetime | None:
    time_obj = obj.get("time")
    if isinstance(time_obj, dict):
        return ms_to_datetime(time_obj.get("created"))
    return None


def _candidate_from_parts(
    parts: list[tuple[str, dict[str, Any]]],
) -> tuple[str | None, tuple[str, ...]]:
    """Join qualifying text parts in ascending part-ID order (spec 5.2)."""
    texts: list[str] = []
    part_ids: list[str] = []
    for part_id, data in sorted(parts, key=lambda item: item[0]):
        if data.get("type") != TEXT_PART_TYPE:
            continue
        if data.get("synthetic") is True or data.get("ignored") is True:
            continue
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        texts.append(text.strip())
        part_ids.append(part_id)
    if not texts:
        return None, ()
    return "\n\n".join(texts), tuple(part_ids)


def _apply_json_thresholds(scan: StoreScan) -> None:
    """Spec 9: over-threshold malformed or unrecognized files sink the store."""
    if scan.file_count and scan.malformed * 10 > scan.file_count:
        scan.supported = False
        scan.diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"
        return
    parsed = scan.file_count - scan.malformed
    if parsed > 0 and scan.unrecognized * 2 > parsed:
        scan.supported = False
        scan.diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"


def _load_json_dict(path: Path, reader: Reader, scan: StoreScan) -> dict[str, Any] | None:
    scan.file_count += 1
    try:
        parsed = json.loads(reader(path))
    except (ValueError, OSError):
        scan.malformed += 1
        return None
    if not isinstance(parsed, dict):
        scan.malformed += 1
        return None
    return parsed


def _subdirectories(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(entry for entry in parent.iterdir() if entry.is_dir() and not entry.is_symlink())


def _json_files(directory: Path, prefix: str) -> list[Path]:
    return sorted(
        entry
        for entry in directory.glob(f"{prefix}*.json")
        if entry.is_file() and not entry.is_symlink()
    )


# -- generation S: SQLite ----------------------------------------------------


def scan_sqlite_store(path: Path, order: int, connect: Connector) -> StoreScan:
    """Parse one ``opencode*.db`` store through a read-only connection."""
    scan = StoreScan(generation=GENERATION_SQLITE, order=order, store_path=path)

    def failed(code: str) -> StoreScan:
        scan.supported = False
        scan.diagnostic_code = code
        scan.sessions.clear()
        scan.messages.clear()
        return scan

    try:
        connection = connect(path)
    except sqlite3.Error:
        return failed("SOURCE_INACCESSIBLE")
    try:
        try:
            return _scan_sqlite_connection(scan, connection)
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower():
                return failed("SOURCE_LOCKED")
            return failed("SOURCE_INACCESSIBLE")
        except sqlite3.DatabaseError:
            return failed("SOURCE_INACCESSIBLE")
    finally:
        connection.close()


def _scan_sqlite_connection(scan: StoreScan, connection: sqlite3.Connection) -> StoreScan:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    def unsupported() -> StoreScan:
        scan.supported = False
        scan.diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"
        scan.sessions.clear()
        scan.messages.clear()
        return scan

    if not set(REQUIRED_COLUMNS) <= tables:
        return unsupported()
    columns: dict[str, set[str]] = {}
    for table, required in REQUIRED_COLUMNS.items():
        # Fixed table names only; never derived from data.
        columns[table] = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required <= columns[table]:
            return unsupported()

    has_version = "version" in columns["session"]
    session_query = "SELECT id, project_id, parent_id, time_created" + (
        ", version" if has_version else ""
    )
    session_query += " FROM session"
    for row in connection.execute(session_query):
        session_id = _string_or_none(row[0])
        project_key = _string_or_none(row[1])
        if session_id is None or project_key is None:
            scan.unrecognized += 1
            continue
        scan.sessions.append(
            SessionCopy(
                generation=GENERATION_SQLITE,
                store_order=scan.order,
                project_key=project_key,
                session_id=session_id,
                parent_id=_string_or_none(row[2]),
                version=_string_or_none(row[4]) if has_version else None,
                created=ms_to_datetime(row[3]),
            )
        )
    session_project = {copy.session_id: copy.project_key for copy in scan.sessions}

    message_rows = connection.execute(
        "SELECT id, session_id, time_created, data FROM message"
    ).fetchall()
    if scan.sessions and not message_rows and tables & V2_TABLE_NAMES:
        # Spec 10.1: v2 layer present, v1 projection empty — fail closed.
        return unsupported()

    parts_by_message: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for row in connection.execute("SELECT id, message_id, data FROM part"):
        part_id = _string_or_none(row[0])
        message_id = _string_or_none(row[1])
        if part_id is None or message_id is None:
            scan.unrecognized += 1
            continue
        try:
            data = json.loads(str(row[2]))
        except ValueError:
            scan.malformed += 1
            continue
        if not isinstance(data, dict):
            scan.malformed += 1
            continue
        parts_by_message.setdefault(message_id, []).append((part_id, data))

    for row in message_rows:
        message_id = _string_or_none(row[0])
        session_id = _string_or_none(row[1])
        if message_id is None or session_id is None:
            scan.unrecognized += 1
            continue
        try:
            data = json.loads(str(row[3]))
        except ValueError:
            scan.malformed += 1
            continue
        if not isinstance(data, dict):
            scan.malformed += 1
            continue
        role = _string_or_none(data.get("role"))
        if role is None:
            scan.unrecognized += 1
            continue
        timestamp = _created_from(data) or ms_to_datetime(row[2])
        parts = parts_by_message.get(message_id, [])
        if role == "user":
            text, part_ids = _candidate_from_parts(parts)
        else:
            text, part_ids = None, ()
        scan.messages.append(
            MessageCopy(
                generation=GENERATION_SQLITE,
                store_order=scan.order,
                project_key=session_project.get(session_id),
                session_id=session_id,
                message_id=message_id,
                role=role,
                timestamp=timestamp,
                candidate_text=text,
                candidate_part_ids=part_ids,
                part_count=len(parts),
            )
        )
    return scan


# -- generation J2: flat global JSON tree ------------------------------------


def scan_j2_store(root: Path, order: int, reader: Reader) -> StoreScan | None:
    """Parse ``<root>/storage`` (spec 3.2). None when the layout is absent."""
    storage = root / "storage"
    session_root = storage / "session"
    if not session_root.is_dir():
        return None
    scan = StoreScan(generation=GENERATION_J2, order=order, store_path=storage)

    project_dirs = _subdirectories(session_root)
    flat_session_files = [
        entry
        for entry in session_root.iterdir()
        if entry.is_file() and entry.name.startswith("ses_")
    ]
    if flat_session_files and not project_dirs:
        # Unknown single-level layout: detected, unsupported, never guessed.
        scan.supported = False
        scan.diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"
        return scan

    for project_dir in project_dirs:
        for session_file in _json_files(project_dir, "ses_"):
            obj = _load_json_dict(session_file, reader, scan)
            if obj is None:
                continue
            session_id = _string_or_none(obj.get("id"))
            if session_id is None or not session_id.startswith("ses_"):
                scan.unrecognized += 1
                continue
            scan.sessions.append(
                SessionCopy(
                    generation=GENERATION_J2,
                    store_order=order,
                    project_key=project_dir.name,
                    session_id=session_id,
                    parent_id=_string_or_none(obj.get("parentID")),
                    version=_string_or_none(obj.get("version")),
                    created=_created_from(obj),
                )
            )
    session_project = {copy.session_id: copy.project_key for copy in scan.sessions}

    parts_by_message: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for message_dir in _subdirectories(storage / "part"):
        for part_file in _json_files(message_dir, "prt_"):
            obj = _load_json_dict(part_file, reader, scan)
            if obj is None:
                continue
            part_id = _string_or_none(obj.get("id")) or part_file.stem
            parts_by_message.setdefault(message_dir.name, []).append((part_id, obj))

    for session_dir in _subdirectories(storage / "message"):
        for message_file in _json_files(session_dir, "msg_"):
            obj = _load_json_dict(message_file, reader, scan)
            if obj is None:
                continue
            message_id = _string_or_none(obj.get("id"))
            role = _string_or_none(obj.get("role"))
            if message_id is None or role is None:
                scan.unrecognized += 1
                continue
            session_id = _string_or_none(obj.get("sessionID")) or session_dir.name
            parts = parts_by_message.get(message_id, [])
            if role == "user":
                text, part_ids = _candidate_from_parts(parts)
            else:
                text, part_ids = None, ()
            scan.messages.append(
                MessageCopy(
                    generation=GENERATION_J2,
                    store_order=order,
                    project_key=session_project.get(session_id),
                    session_id=session_id,
                    message_id=message_id,
                    role=role,
                    timestamp=_created_from(obj),
                    candidate_text=text,
                    candidate_part_ids=part_ids,
                    part_count=len(parts),
                )
            )
    _apply_json_thresholds(scan)
    return scan


# -- generation J1: per-project JSON tree ------------------------------------


def _j1_slug_dirs(root: Path) -> list[Path]:
    return [
        slug_dir
        for slug_dir in _subdirectories(root / "project")
        if (slug_dir / "storage" / "session" / "info").is_dir()
    ]


def scan_j1_store(root: Path, order: int, reader: Reader) -> StoreScan | None:
    """Parse ``<root>/project/<slug>`` trees (spec 3.3). None when absent."""
    slug_dirs = _j1_slug_dirs(root)
    if not slug_dirs:
        return None
    scan = StoreScan(generation=GENERATION_J1, order=order, store_path=root / "project")
    for slug_dir in slug_dirs:
        _scan_j1_slug(scan, slug_dir, reader)
    _apply_json_thresholds(scan)
    return scan


def _scan_j1_slug(scan: StoreScan, slug_dir: Path, reader: Reader) -> None:
    slug = slug_dir.name
    base = slug_dir / "storage" / "session"
    for session_file in _json_files(base / "info", "ses_"):
        obj = _load_json_dict(session_file, reader, scan)
        if obj is None:
            continue
        session_id = _string_or_none(obj.get("id"))
        if session_id is None or not session_id.startswith("ses_"):
            scan.unrecognized += 1
            continue
        scan.sessions.append(
            SessionCopy(
                generation=GENERATION_J1,
                store_order=scan.order,
                project_key=slug,
                session_id=session_id,
                parent_id=_string_or_none(obj.get("parentID")),
                version=_string_or_none(obj.get("version")),
                created=_created_from(obj),
            )
        )
    for session_dir in _subdirectories(base / "message"):
        for message_file in _json_files(session_dir, "msg_"):
            obj = _load_json_dict(message_file, reader, scan)
            if obj is None:
                continue
            message_id = _string_or_none(obj.get("id"))
            role = _string_or_none(obj.get("role"))
            if message_id is None or role is None:
                scan.unrecognized += 1
                continue
            session_id = _string_or_none(obj.get("sessionID")) or session_dir.name
            parts = _j1_message_parts(scan, base, session_id, message_id, obj, reader)
            if parts is None:
                # No inline parts and no split-part directory: none of the
                # three J1 sub-shapes (spec 3.3) — counted, never guessed.
                scan.unrecognized += 1
                continue
            if role == "user":
                text, part_ids = _candidate_from_parts(parts)
            else:
                text, part_ids = None, ()
            scan.messages.append(
                MessageCopy(
                    generation=GENERATION_J1,
                    store_order=scan.order,
                    project_key=slug,
                    session_id=session_id,
                    message_id=message_id,
                    role=role,
                    timestamp=_created_from(obj),
                    candidate_text=text,
                    candidate_part_ids=part_ids,
                    part_count=len(parts),
                )
            )


def _j1_message_parts(
    scan: StoreScan,
    base: Path,
    session_id: str,
    message_id: str,
    message_obj: dict[str, Any],
    reader: Reader,
) -> list[tuple[str, dict[str, Any]]] | None:
    """Parts of one J1 message: split files (J1c) win over inline (J1a/J1b)."""
    split_dir = base / "part" / session_id / message_id
    if split_dir.is_dir():
        parts: list[tuple[str, dict[str, Any]]] = []
        for part_file in _json_files(split_dir, "prt_"):
            obj = _load_json_dict(part_file, reader, scan)
            if obj is None:
                continue
            part_id = _string_or_none(obj.get("id")) or part_file.stem
            parts.append((part_id, obj))
        return parts
    inline = message_obj.get("parts")
    if isinstance(inline, list):
        parts = []
        for index, entry in enumerate(inline):
            if not isinstance(entry, dict):
                continue
            part_id = _string_or_none(entry.get("id")) or f"{message_id}-inline-{index:03d}"
            parts.append((part_id, entry))
        return parts
    return None


# -- cross-generation deduplication (spec 7) ---------------------------------


def _precedence(scan: StoreScan) -> tuple[int, int]:
    return (-_GENERATION_PRIORITY[scan.generation], scan.order)


def canonicalize(
    scans: list[StoreScan],
) -> tuple[dict[str, SessionCopy], dict[tuple[str, str], tuple[MessageCopy, bool]]]:
    """Collapse record copies by preserved ID across stores and generations.

    Sessions: the highest-precedence copy wins. Messages: when candidate
    texts agree the highest-precedence copy wins; when they differ the copy
    with more parts wins and the record is flagged (spec 7.1).
    """
    ordered = sorted((scan for scan in scans if scan.supported), key=_precedence)
    sessions: dict[str, SessionCopy] = {}
    for scan in ordered:
        for session in scan.sessions:
            sessions.setdefault(session.session_id, session)
    messages: dict[tuple[str, str], tuple[MessageCopy, bool]] = {}
    for scan in ordered:
        for message in scan.messages:
            key = (message.session_id, message.message_id)
            current = messages.get(key)
            if current is None:
                messages[key] = (message, False)
                continue
            best, _ = current
            if (best.candidate_text or "") == (message.candidate_text or ""):
                continue
            winner = message if message.part_count > best.part_count else best
            messages[key] = (winner, True)
    return sessions, messages


def build_candidates(
    sessions: dict[str, SessionCopy],
    messages: dict[tuple[str, str], tuple[MessageCopy, bool]],
) -> list[Candidate]:
    """Apply the spec 5 inclusion rules to the canonical record set."""
    candidates: list[Candidate] = []
    for (session_id, message_id), (copy, mismatch) in messages.items():
        session = sessions.get(session_id)
        if session is None:
            # Orphan message: session eligibility cannot be verified.
            continue
        if session.parent_id is not None:
            continue
        if copy.role != "user":
            continue
        if not copy.candidate_text:
            continue
        if copy.candidate_text in VENDOR_TEMPLATE_TEXTS:
            continue
        candidates.append(
            Candidate(
                project_key=copy.project_key or session.project_key,
                session_id=session_id,
                message_id=message_id,
                text=copy.candidate_text,
                part_ids=copy.candidate_part_ids,
                timestamp=copy.timestamp,
                generation_text_mismatch=mismatch,
                session_version=session.version,
            )
        )
    candidates.sort(
        key=lambda item: (item.timestamp or _SORT_SENTINEL, item.session_id, item.message_id)
    )
    return candidates


def canonical_record_counts(
    sessions: dict[str, SessionCopy],
    messages: dict[tuple[str, str], tuple[MessageCopy, bool]],
) -> dict[str, int]:
    """Canonical message-record count per project key, any role."""
    counts: dict[str, int] = {}
    for (session_id, _), (copy, _) in messages.items():
        session = sessions.get(session_id)
        project = copy.project_key or (session.project_key if session else None)
        if project is None:
            continue
        counts[project] = counts.get(project, 0) + 1
    return counts


def canonical_versions(sessions: dict[str, SessionCopy]) -> dict[str, set[str]]:
    """Writing-app versions seen per project, from canonical session copies."""
    versions: dict[str, set[str]] = {}
    for session in sessions.values():
        if session.version is not None:
            versions.setdefault(session.project_key, set()).add(session.version)
    return versions


def project_keys(
    sessions: dict[str, SessionCopy],
    messages: dict[tuple[str, str], tuple[MessageCopy, bool]],
) -> set[str]:
    """Every (root, projectID) instance key seen in the canonical record set."""
    keys = {session.project_key for session in sessions.values()}
    keys.update(project for project in canonical_record_counts(sessions, messages) if project)
    return keys


# -- snapshot enumeration ----------------------------------------------------


def list_snapshot_source_files(root: Path) -> list[Path]:
    """Relative paths of every allowlisted JSON-generation file under ``root``.

    Mirrors the spec 2.2 allowlist exactly; nothing else is ever copied.
    """
    found: list[Path] = []
    migration = root / "storage" / "migration"
    if migration.is_file() and not migration.is_symlink():
        found.append(migration)
    storage = root / "storage"
    for parent, prefix in (
        (storage / "session", "ses_"),
        (storage / "message", "msg_"),
        (storage / "part", "prt_"),
    ):
        for subdirectory in _subdirectories(parent):
            found.extend(_json_files(subdirectory, prefix))
    for slug_dir in _j1_slug_dirs(root):
        base = slug_dir / "storage" / "session"
        found.extend(_json_files(base / "info", "ses_"))
        for session_dir in _subdirectories(base / "message"):
            found.extend(_json_files(session_dir, "msg_"))
        for session_dir in _subdirectories(base / "part"):
            for message_dir in _subdirectories(session_dir):
                found.extend(_json_files(message_dir, "prt_"))
    return [path.relative_to(root) for path in found]


# -- snapshot sanitize (spec 8) ----------------------------------------------


def sanitize_snapshot_database(path: Path) -> int:
    """Strip every non-allowlisted object from a snapshot database, then VACUUM.

    Runs only against the private snapshot copy, never against source data.
    Returns the number of dropped objects; raises on any failure so the
    caller can delete the snapshot instead of proceeding unsanitized.
    """
    connection = sqlite3.connect(path)
    connection.isolation_level = None
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        objects = connection.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view', 'trigger')"
        ).fetchall()
        dropped = 0
        for kind_filter in ("trigger", "view", "table"):
            for raw_name, raw_kind in objects:
                name = str(raw_name)
                kind = str(raw_kind)
                if kind != kind_filter or name.startswith("sqlite_"):
                    continue
                if kind == "table" and name in SNAPSHOT_KEEP_TABLES:
                    continue
                quoted = name.replace('"', '""')
                connection.execute(f'DROP {kind.upper()} IF EXISTS "{quoted}"')
                dropped += 1
        connection.execute("VACUUM")
    finally:
        connection.close()
    return dropped
