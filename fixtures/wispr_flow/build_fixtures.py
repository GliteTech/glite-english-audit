"""Regenerate the committed synthetic Wispr Flow fixture databases.

Run from the repository root:

    uv run python fixtures/wispr_flow/build_fixtures.py

Every value is synthetic. Secret-looking strings carry the FAKE marker, and
content that must never surface in adapter output carries a SENTINEL marker
so tests can assert its absence. The schema mirrors
``specifications/sources/wispr_flow.md`` section 3.2.
"""

import sqlite3
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path("home") / "Library" / "Application Support" / "Wispr Flow"

HISTORY_FULL_DDL = """
CREATE TABLE "History" (
    "transcriptEntityId" VARCHAR(36) PRIMARY KEY,
    "asrText" TEXT,
    "timestamp" DATETIME,
    "app" VARCHAR(255),
    "language" TEXT,
    "conversationId" VARCHAR(255),
    "status" VARCHAR(255),
    "isArchived" TINYINT(1),
    "appVersion" TEXT,
    "numWords" INTEGER,
    "duration" FLOAT,
    "formattedText" TEXT,
    "editedText" TEXT,
    "textboxContents" TEXT,
    "axText" TEXT,
    "additionalContext" TEXT,
    "url" TEXT,
    "audio" BLOB,
    "screenshot" BLOB
)
"""

SEQUELIZE_DDL = 'CREATE TABLE "SequelizeMeta" ("name" VARCHAR(255) PRIMARY KEY)'

MIGRATIONS_2024 = [
    "20240521010101-create-history.js",
    "20240601010101-create-dictionary.js",
]
MIGRATIONS_CURRENT = [
    *MIGRATIONS_2024,
    "20240815010101-add-conversation-id.js",
    "20241102010101-add-tone-matching.js",
    "20250110010101-add-fallback-asr.js",
    "20250405010101-create-notifications.js",
    "20250512010101-create-notes.js",
    "20250808010101-create-snippets.js",
    "20260115010101-create-polish.js",
    "20260301010101-create-note-versions.js",
    "20260405010101-create-meetings.js",
    "20260412010101-create-links.js",
]

SENTINEL_COLUMNS = {
    "formattedText": "SENTINEL-FORMATTED-NEVER-EXTRACT",
    "editedText": "SENTINEL-EDITED-NEVER-EXTRACT",
    "textboxContents": "SENTINEL-CLIPBOARD-NEVER-EXTRACT",
    "axText": "SENTINEL-AX-NEVER-EXTRACT",
    "additionalContext": "SENTINEL-CONTEXT-NEVER-EXTRACT",
    "url": "https://example.invalid/SENTINEL-URL-NEVER-EXTRACT",
}


def _tid(index: int) -> str:
    return f"aaaaaaaa-{index:04d}-4aaa-8aaa-{index:012d}"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA page_size = 1024")
    return connection


def _insert_full_row(
    connection: sqlite3.Connection,
    *,
    index: int,
    asr_text: str | None,
    timestamp: str,
    app: str | None = None,
    conversation_id: str | None = None,
    status: str | None = "formatted",
    is_archived: int = 0,
    app_version: str | None = "1.5.308",
    num_words: int | None = None,
) -> None:
    connection.execute(
        'INSERT INTO "History" ("transcriptEntityId", "asrText", "timestamp", "app", '
        '"language", "conversationId", "status", "isArchived", "appVersion", "numWords", '
        '"duration", "formattedText", "editedText", "textboxContents", "axText", '
        '"additionalContext", "url", "audio", "screenshot") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _tid(index),
            asr_text,
            timestamp,
            app,
            "en",
            conversation_id,
            status,
            is_archived,
            app_version,
            num_words,
            3.5,
            SENTINEL_COLUMNS["formattedText"],
            SENTINEL_COLUMNS["editedText"],
            SENTINEL_COLUMNS["textboxContents"],
            SENTINEL_COLUMNS["axText"],
            SENTINEL_COLUMNS["additionalContext"],
            SENTINEL_COLUMNS["url"],
            b"FAKE-AUDIO-BYTES",
            b"FAKE-SCREENSHOT-BYTES",
        ),
    )


def _add_sentinel_tables(connection: sqlite3.Connection) -> None:
    connection.execute('CREATE TABLE "Dictionary" ("word" TEXT, "replacement" TEXT)')
    connection.execute(
        'INSERT INTO "Dictionary" VALUES (?, ?)',
        ("SENTINEL-DICTIONARY-WORD", "Kubernetes"),
    )
    connection.execute('CREATE TABLE "polish" ("instruction" TEXT, "output" TEXT)')
    connection.execute(
        'INSERT INTO "polish" VALUES (?, ?)',
        ("SENTINEL-POLISH-INSTRUCTION", "SENTINEL-POLISH-OUTPUT"),
    )
    connection.execute('CREATE TABLE "notes" ("content" TEXT)')
    connection.execute('INSERT INTO "notes" VALUES (?)', ("SENTINEL-NOTE-CONTENT",))
    connection.execute('CREATE TABLE "meetings" ("transcript" TEXT)')
    connection.execute(
        'INSERT INTO "meetings" VALUES (?)',
        ("SENTINEL-MEETING-TRANSCRIPT other speakers",),
    )
    connection.execute('CREATE TABLE "links" ("url" TEXT)')
    connection.execute(
        'INSERT INTO "links" VALUES (?)',
        ("https://example.invalid/SENTINEL-LINK",),
    )
    connection.execute('CREATE TABLE "FutureWidget" ("payload" TEXT)')
    connection.execute('INSERT INTO "FutureWidget" VALUES (?)', ("SENTINEL-UNKNOWN-TABLE",))


def _add_migrations(connection: sqlite3.Connection, names: list[str]) -> None:
    connection.execute(SEQUELIZE_DDL)
    connection.executemany('INSERT INTO "SequelizeMeta" VALUES (?)', [(n,) for n in names])


def build_success(db_path: Path) -> None:
    connection = _connect(db_path)
    connection.execute(HISTORY_FULL_DDL)
    rows: list[dict[str, object]] = [
        {
            "asr_text": "I very like this plan, let us start from the first step.",
            "timestamp": "2026-06-01 10:00:00.000 +00:00",
            "app": "com.microsoft.VSCode",
            "conversation_id": "conv-alpha",
            "num_words": 12,
        },
        {
            "asr_text": "Please explain me how this function works, I am not understanding it.",
            "timestamp": "2026-06-01 10:01:00.000 +00:00",
            "app": "com.microsoft.VSCode",
            "conversation_id": "conv-alpha",
        },
        {
            "asr_text": "Yesterday I have wrote the report and sended it to my colleague.",
            "timestamp": "2026-06-01 10:05:00.000 +00:00",
            "app": "com.apple.mail",
        },
        {
            "asr_text": "How I can improve my English speaking without a teacher?",
            "timestamp": "2026-06-01 10:10:00.000 +00:00",
            "app": "com.unknownvendor.dictpad",
            "status": "",
            "num_words": 50,
        },
        {
            "asr_text": "I am agree with the last comment, we should discuss it tomorrow.",
            "timestamp": "2026-06-01 10:15:00.000 +00:00",
            "status": "dismissed",
        },
        {
            "asr_text": "Make this text more shorter and more polite, please.",
            "timestamp": "2026-06-01 10:20:00.000 +00:00",
            "app": "com.google.Chrome",
            "status": "extension_paste",
        },
        {
            "asr_text": "This are my notes from the morning stand up meeting.",
            "timestamp": "2026-06-01 10:25:00.000 +00:00",
            "app": "com.tinyspeck.slackmacgap",
            "is_archived": 1,
        },
        {
            "asr_text": "I want to say thank you for helping me with this difficult task.",
            "timestamp": "2026-06-02T09:30:00+02:00",
            "app": "com.apple.notes",
        },
        {
            "asr_text": "Since two years I am using this dictation application every day.",
            "timestamp": "not-a-real-timestamp",
            "app": "com.apple.Terminal",
        },
        {
            "asr_text": "The weather today is very nice, we can to go outside for lunch.",
            "timestamp": "2026-06-02 08:00:00.000 +00:00",
            "status": "mystery_status",
        },
        {
            "asr_text": None,
            "timestamp": "2026-06-01 09:00:00.000 +00:00",
            "status": "empty",
        },
        {
            "asr_text": "",
            "timestamp": "2026-06-01 09:01:00.000 +00:00",
            "status": "no_audio",
        },
    ]
    for index, row in enumerate(rows, start=1):
        _insert_full_row(connection, index=index, **row)  # type: ignore[arg-type]
    _add_sentinel_tables(connection)
    _add_migrations(connection, MIGRATIONS_CURRENT)
    connection.commit()
    connection.close()


def build_empty(db_path: Path) -> None:
    connection = _connect(db_path)
    connection.execute(HISTORY_FULL_DDL)
    _insert_full_row(
        connection,
        index=1,
        asr_text=None,
        timestamp="2026-05-01 08:00:00.000 +00:00",
        status="empty",
    )
    _insert_full_row(
        connection,
        index=2,
        asr_text="",
        timestamp="2026-05-01 08:05:00.000 +00:00",
        status="no_audio",
    )
    _add_migrations(connection, MIGRATIONS_CURRENT)
    connection.commit()
    connection.close()


def build_malformed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(
        b"Not a SQLite database. SYNTHETIC FAKE PLACEHOLDER bytes for the "
        b"malformed wispr_flow fixture.\n"
    )


def build_unsupported(db_path: Path) -> None:
    connection = _connect(db_path)
    # Plausible-but-unknown generation: the required asrText column was
    # renamed, so the fingerprint must fail closed.
    connection.execute(
        'CREATE TABLE "History" ('
        '"transcriptEntityId" VARCHAR(36) PRIMARY KEY, '
        '"recognizedSpeech" TEXT, '
        '"timestamp" DATETIME, '
        '"status" VARCHAR(255))'
    )
    connection.executemany(
        'INSERT INTO "History" VALUES (?, ?, ?, ?)',
        [
            (
                "bbbbbbbb-0001-4bbb-8bbb-000000000001",
                "SENTINEL-UNSUPPORTED-NEVER-EXTRACT-1",
                "2027-01-01 10:00:00.000 +00:00",
                "formatted",
            ),
            (
                "bbbbbbbb-0002-4bbb-8bbb-000000000002",
                "SENTINEL-UNSUPPORTED-NEVER-EXTRACT-2",
                "2027-01-01 10:05:00.000 +00:00",
                "formatted",
            ),
        ],
    )
    _add_migrations(connection, [*MIGRATIONS_CURRENT, "20270101010101-rename-asr-text.js"])
    connection.commit()
    connection.close()


def build_migration(db_path: Path) -> None:
    connection = _connect(db_path)
    # Older-but-supported 2024 generation: required columns plus only two
    # optional ones, and no post-2025 tables.
    connection.execute(
        'CREATE TABLE "History" ('
        '"transcriptEntityId" VARCHAR(36) PRIMARY KEY, '
        '"asrText" TEXT, '
        '"timestamp" DATETIME, '
        '"app" VARCHAR(255), '
        '"status" VARCHAR(255))'
    )
    rows = [
        (
            "cccccccc-0001-4ccc-8ccc-000000000001",
            "Can you check my text for mistakes, I wrote it very quick.",
            "2024-06-10 09:00:00.000 +00:00",
            "notepad.exe",
            "formatted",
        ),
        (
            "cccccccc-0002-4ccc-8ccc-000000000002",
            "I am not sure how to say this correct in English.",
            "2024-06-10 09:05:00.000 +00:00",
            None,
            "formatted",
        ),
        (
            "cccccccc-0003-4ccc-8ccc-000000000003",
            "We discussed about the project and decided to continue next week.",
            "2024-06-11 08:00:00.000 +00:00",
            "notepad.exe",
            None,
        ),
    ]
    connection.executemany('INSERT INTO "History" VALUES (?, ?, ?, ?, ?)', rows)
    connection.execute('CREATE TABLE "Dictionary" ("word" TEXT)')
    connection.execute('INSERT INTO "Dictionary" VALUES (?)', ("SENTINEL-DICTIONARY-WORD",))
    _add_migrations(connection, MIGRATIONS_2024)
    connection.commit()
    connection.close()


def main() -> None:
    builders = {
        "success": build_success,
        "empty": build_empty,
        "malformed": build_malformed,
        "unsupported": build_unsupported,
        "migration": build_migration,
    }
    for variant, builder in builders.items():
        db_path = FIXTURES_DIR / variant / DATA_ROOT / "flow.sqlite"
        builder(db_path)
        print(f"built {db_path.relative_to(FIXTURES_DIR)} ({db_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
