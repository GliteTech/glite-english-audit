"""SQLite structure inspection and row scanning for the Wispr Flow store.

Everything here runs against a consistent snapshot copy of ``flow.sqlite``,
never against the live database. Reads are allowlist-driven: the ``History``
table through the fixed column allowlist, plus ``sqlite_master`` and
``SequelizeMeta`` for structure only (specification sections 3-4). SELECT
statements name allowlisted columns explicitly; ``SELECT *`` is never used
because the same table carries never-ingest columns.
"""

import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from glite_english_audit.normalization.tokenizer import count_words

# Spec 3.2: starred required columns. A store missing any of them is
# unsupported, never guessed at.
REQUIRED_COLUMNS: tuple[str, ...] = ("transcriptEntityId", "asrText", "timestamp")

# Spec 3.2: optional allowlist columns, feature-detected per store.
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "app",
    "language",
    "conversationId",
    "status",
    "isArchived",
    "appVersion",
    "numWords",
    "duration",
)

# Spec 3.2 never-ingest denylist (lowercase). verify() audits every executed
# statement against these names; unknown columns are equally forbidden because
# SELECTs name allowlisted columns only.
NEVER_INGEST_COLUMNS: frozenset[str] = frozenset(
    {
        "formattedtext",
        "editedtext",
        "tonematchedtext",
        "defaultformattedtext",
        "fallbackformattedtext",
        "defaultasrtext",
        "fallbackasrtext",
        "pastedtext",
        "textboxcontents",
        "axtext",
        "axhtml",
        "additionalcontext",
        "url",
        "audio",
        "builtinaudio",
        "screenshot",
        "opuschunks",
        "usereditmetadata",
        "tonematchpairs",
        "personalizationstylesettings",
        "feedback",
        "editedtextstatus",
        "editedtextattempts",
        "numwordscorrected",
        "numdictionaryreplacements",
        "needsuploading",
        "sharetype",
        "micdevice",
        "e2elatency",
        "speechduration",
        "averagelogprob",
        "formattingdivergencescore",
        "usedfallbackasr",
        "usedfallbackformatting",
        "timezoneoffsetminutes",
    }
)

# Spec 3.3: tables whose presence feeds the fingerprint; rows never selected.
KNOWN_EXTRA_TABLES: frozenset[str] = frozenset(
    {
        "dictionary",
        "polish",
        "notes",
        "note_versions",
        "note_images",
        "meetings",
        "meeting_versions",
        "calendar_events",
        "links",
        "snippets",
        "notifications",
        "remotenotifications",
    }
)

# Spec 3.2 observed status values (not exhaustive). Unknown values never gate
# inclusion; they are only counted for research refresh.
KNOWN_STATUS_VALUES: frozenset[str] = frozenset(
    {"formatted", "", "empty", "no_audio", "dismissed", "extension_paste", "extension_other"}
)

_STATUS_FLAGS: dict[str, str] = {
    "dismissed": "dismissed",
    "extension_paste": "command_mode",
    "extension_other": "command_mode",
}

# Spec 4.2: fixed local bundle-ID map to a coarse destination category.
# Unmapped non-empty IDs become "other"; the raw ID never leaves the adapter.
_DESTINATION_APP_MAP: dict[str, str] = {
    "com.microsoft.vscode": "code_editor",
    "com.apple.dt.xcode": "code_editor",
    "com.todesktop.230313mzl4w4u92": "code_editor",
    "com.apple.mail": "email",
    "com.microsoft.outlook": "email",
    "com.google.chrome": "browser",
    "com.apple.safari": "browser",
    "org.mozilla.firefox": "browser",
    "com.tinyspeck.slackmacgap": "messaging",
    "com.hnc.discord": "messaging",
    "ru.keepcoder.telegram": "messaging",
    "com.apple.terminal": "terminal",
    "com.googlecode.iterm2": "terminal",
    "com.apple.notes": "notes",
    "notion.id": "notes",
    "com.microsoft.word": "documents",
    "com.apple.iwork.pages": "documents",
}

# "YYYY-MM-DD HH:MM:SS.mmm +00:00" — the observed store form has a space
# before the offset, which fromisoformat rejects.
_OFFSET_TAIL = re.compile(r"^(?P<body>.+?)\s+(?P<offset>[+-]\d{2}:?\d{2})$")


def _quote(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def parse_timestamp(raw: object) -> datetime | None:
    """Parse the spec 5 timestamp forms; unparsable values become ``None``."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    candidates = [text]
    match = _OFFSET_TAIL.fullmatch(text)
    if match:
        candidates.append(match.group("body") + match.group("offset"))
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # Observed stores write UTC text; naive ISO forms are read as UTC.
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def destination_app(raw: object) -> str | None:
    """Coarse local-only destination category for a bundle or app ID."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _DESTINATION_APP_MAP.get(raw.strip().lower(), "other")


@dataclass(frozen=True)
class StoreStructure:
    """Structure-only fingerprint inputs for one store."""

    history_table: str | None
    columns: dict[str, str]
    missing_required: tuple[str, ...]
    migration_count: int | None
    known_extra_tables: int
    unknown_extra_tables: int

    @property
    def supported(self) -> bool:
        return self.history_table is not None and not self.missing_required


def inspect_structure(connection: sqlite3.Connection, statement_log: list[str]) -> StoreStructure:
    """Read table and column names only; never any row content."""
    tables_sql = "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    statement_log.append(tables_sql)
    tables = [str(row[0]) for row in connection.execute(tables_sql)]
    lower_tables = {table.lower(): table for table in tables}

    known_extra = 0
    unknown_extra = 0
    for lower in lower_tables:
        if lower in {"history", "sequelizemeta"} or lower.startswith("sqlite_"):
            continue
        if lower in KNOWN_EXTRA_TABLES:
            known_extra += 1
        else:
            unknown_extra += 1

    history = lower_tables.get("history")
    columns: dict[str, str] = {}
    missing_required: tuple[str, ...] = REQUIRED_COLUMNS
    if history is not None:
        info_sql = f"PRAGMA table_info({_quote(history)})"
        statement_log.append(info_sql)
        actual = {str(row[1]).lower(): str(row[1]) for row in connection.execute(info_sql)}
        for name in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS):
            if name.lower() in actual:
                columns[name.lower()] = actual[name.lower()]
        missing_required = tuple(name for name in REQUIRED_COLUMNS if name.lower() not in columns)

    migration_count: int | None = None
    meta_table = lower_tables.get("sequelizemeta")
    if meta_table is not None:
        count_sql = f"SELECT COUNT(*) FROM {_quote(meta_table)}"
        statement_log.append(count_sql)
        migration_count = int(connection.execute(count_sql).fetchone()[0])

    return StoreStructure(
        history_table=history,
        columns=columns,
        missing_required=missing_required,
        migration_count=migration_count,
        known_extra_tables=known_extra,
        unknown_extra_tables=unknown_extra,
    )


def structure_fingerprint(structure: StoreStructure) -> str:
    """Deterministic, content-free schema fingerprint (spec 6.1)."""
    if structure.history_table is None:
        return "unsupported:no-history"
    if structure.missing_required:
        return "unsupported:missing-required"
    optional_present = sum(1 for name in OPTIONAL_COLUMNS if name.lower() in structure.columns)
    migrations = (
        f"mig{structure.migration_count}" if structure.migration_count is not None else "mig-none"
    )
    return (
        f"history-2024/opt{optional_present}of{len(OPTIONAL_COLUMNS)}/{migrations}"
        f"/extra{structure.known_extra_tables}+{structure.unknown_extra_tables}"
    )


@dataclass(frozen=True)
class HistoryRow:
    """One kept candidate row (spec 4.2), already reduced to safe fields."""

    transcript_entity_id: str
    text: str
    word_count: int
    timestamp: datetime | None
    conversation_id: str | None
    destination_app: str | None
    app_version: str | None
    content_flags: tuple[str, ...]


@dataclass(frozen=True)
class StoreScan:
    """Aggregate result of one full History scan."""

    rows: tuple[HistoryRow, ...]
    empty_asr_rows: int
    empty_by_status: dict[str, int] = field(default_factory=dict)
    pk_anomalies: int = 0
    unknown_status_rows: int = 0
    wordcount_divergent_rows: int = 0
    app_versions: tuple[str, ...] = ()

    @property
    def total_rows(self) -> int:
        return len(self.rows) + self.pk_anomalies + self.empty_asr_rows

    @property
    def pk_anomaly_ratio(self) -> float:
        """Anomalous share of would-be candidate rows (spec 8: 1% limit)."""
        seen = len(self.rows) + self.pk_anomalies
        return self.pk_anomalies / seen if seen else 0.0


def scan_history(
    connection: sqlite3.Connection,
    structure: StoreStructure,
    statement_log: list[str],
) -> StoreScan:
    """Scan History through the column allowlist; identical for every caller.

    Discovery and extraction both call this, so the candidate set can never
    diverge between the inventory and the extracted utterances.
    """
    if not structure.supported or structure.history_table is None:
        msg = "scan_history requires a fingerprint-supported store"
        raise ValueError(msg)
    table = _quote(structure.history_table)
    tid = _quote(structure.columns["transcriptentityid"])
    asr = _quote(structure.columns["asrtext"])
    timestamp_col = _quote(structure.columns["timestamp"])

    pk_sql = f"SELECT {tid} FROM {table}"
    statement_log.append(pk_sql)
    pk_counts = Counter(row[0] for row in connection.execute(pk_sql))

    selected_lower = ["transcriptentityid", "asrtext", "timestamp"]
    for name in ("app", "language", "conversationId", "status", "isArchived", "appVersion"):
        if name.lower() in structure.columns:
            selected_lower.append(name.lower())
    if "numwords" in structure.columns:
        selected_lower.append("numwords")
    select_list = ", ".join(_quote(structure.columns[name]) for name in selected_lower)
    candidate_sql = (
        f"SELECT {select_list} FROM {table} "
        f"WHERE {asr} IS NOT NULL AND TRIM({asr}) != '' "
        f"ORDER BY {timestamp_col}, {tid}"
    )
    statement_log.append(candidate_sql)

    kept: list[HistoryRow] = []
    empty_rows = 0
    empty_by_status: dict[str, int] = {}
    pk_anomalies = 0
    unknown_status = 0
    divergent = 0
    versions: set[str] = set()
    for row in connection.execute(candidate_sql):
        value = dict(zip(selected_lower, row, strict=True))
        text = value["asrtext"]
        if not isinstance(text, str) or not text.strip():
            # SQL TRIM is ASCII-only; Unicode-whitespace-only rows are empty.
            empty_rows += 1
            _count_status(value.get("status"), empty_by_status)
            continue
        status = value.get("status")
        if isinstance(status, str) and status not in KNOWN_STATUS_VALUES:
            unknown_status += 1
        raw_tid = value["transcriptentityid"]
        tid_text = "" if raw_tid is None else str(raw_tid).strip()
        if not tid_text or pk_counts[raw_tid] > 1:
            pk_anomalies += 1
            continue
        flags: list[str] = []
        if bool(value.get("isarchived")):
            flags.append("archived")
        if isinstance(status, str) and status in _STATUS_FLAGS:
            flags.append(_STATUS_FLAGS[status])
        try:
            uuid.UUID(tid_text)
        except ValueError:
            flags.append("nonuuid_pk")
        parsed_timestamp = parse_timestamp(value["timestamp"])
        if parsed_timestamp is None:
            flags.append("undated")
        app_version = value.get("appversion")
        if isinstance(app_version, str) and app_version.strip():
            versions.add(app_version.strip())
        word_count = count_words(text)
        num_words = value.get("numwords")
        if (
            isinstance(num_words, int)
            and num_words > 0
            and word_count > 0
            and max(num_words, word_count) > 3 * min(num_words, word_count)
        ):
            divergent += 1
        conversation = value.get("conversationid")
        kept.append(
            HistoryRow(
                transcript_entity_id=tid_text,
                text=text,
                word_count=word_count,
                timestamp=parsed_timestamp,
                conversation_id=(
                    conversation.strip()
                    if isinstance(conversation, str) and conversation.strip()
                    else None
                ),
                destination_app=destination_app(value.get("app")),
                app_version=app_version if isinstance(app_version, str) else None,
                content_flags=tuple(flags),
            )
        )

    status_expr = _quote(structure.columns["status"]) if "status" in structure.columns else "NULL"
    empty_sql = f"SELECT {status_expr} FROM {table} WHERE {asr} IS NULL OR TRIM({asr}) = ''"
    statement_log.append(empty_sql)
    for row in connection.execute(empty_sql):
        empty_rows += 1
        status = row[0]
        _count_status(status, empty_by_status)
        if isinstance(status, str) and status not in KNOWN_STATUS_VALUES:
            unknown_status += 1

    return StoreScan(
        rows=tuple(kept),
        empty_asr_rows=empty_rows,
        empty_by_status=empty_by_status,
        pk_anomalies=pk_anomalies,
        unknown_status_rows=unknown_status,
        wordcount_divergent_rows=divergent,
        app_versions=tuple(sorted(versions)),
    )


def _count_status(status: object, empty_by_status: dict[str, int]) -> None:
    key = status if isinstance(status, str) else "<null>"
    empty_by_status[key] = empty_by_status.get(key, 0) + 1
