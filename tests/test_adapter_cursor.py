"""Fixture-driven tests for the Cursor source adapter (beta, inventory only)."""

import hashlib
import json
import shutil
import sqlite3
import stat
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from glite_english_audit.adapters.cursor import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BETA_NO_TEXT_REASON,
    CursorAdapter,
    create_adapter,
)
from glite_english_audit.adapters.cursor import adapter as cursor_adapter
from glite_english_audit.adapters.cursor.adapter import DENY_DIR_NAMES, DENY_FILE_NAMES
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.models import NormalizedUtterance, SourceInstanceRecord
from glite_english_audit.discovery.base import DiscoveryContext, DiscoveryOutcome
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.verification.fixture_policy import load_fixture_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures" / "cursor"
USER_RELATIVE = Path("Library") / "Application Support" / "Cursor" / "User"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

SUCCESS_TEXTS = {
    "I very like this plan, we should start from the login page.",
    "Please explain me why the build is failing on CI.",
    "How I can center this div without to use flexbox?",
    "I did not received the webhook event, what we should check first?",
    "Since many years I write code, but my English comments are still not so good.",
    "Can you help me to make this error message more polite for the users?",
    "I am agree with your suggestion, please apply it to all files.",
    "Yesterday I have deployed the fix and now it works much more better.",
}
WORKSPACE_DIR_NAME = "a1b2c3d4e5f60718aabbccdd00112233"
WORKSPACE_FOLDER = "file:///Users/synthetic-user/projects/fake-webapp"


def _materialize_databases(home: Path) -> None:
    """Build every committed ``*.vscdb.sql`` text file into a real SQLite DB."""
    for sql_path in sorted(home.rglob("*.vscdb.sql")):
        database_path = sql_path.with_suffix("")
        connection = sqlite3.connect(database_path)
        connection.executescript(sql_path.read_text(encoding="utf-8"))
        connection.commit()
        connection.close()
        sql_path.unlink()


def _build_home(variant: str, destination: Path) -> Path:
    home = destination / "home"
    shutil.copytree(FIXTURES / variant / "home", home)
    _materialize_databases(home)
    return home


def _context(
    home: Path,
    environ: dict[str, str] | None = None,
    os_environment: OsEnvironment = OsEnvironment.MACOS,
) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=os_environment, home=home, now=NOW, environ=environ or {}
    )


def _single_record(outcome: DiscoveryOutcome) -> SourceInstanceRecord:
    assert len(outcome.records) == 1
    return outcome.records[0]


def _discover(variant: str, tmp_path: Path) -> tuple[CursorAdapter, DiscoveryOutcome, Path]:
    home = _build_home(variant, tmp_path / variant)
    adapter = CursorAdapter()
    return adapter, adapter.discover(_context(home)), home


def test_fixture_meta_declarations() -> None:
    for variant, kind in (
        ("success", "success"),
        ("empty", "empty"),
        ("malformed", "malformed"),
        ("unsupported", "unsupported"),
        ("migration", "migration"),
    ):
        meta = load_fixture_meta(FIXTURES / variant)
        assert meta.adapter_id == ADAPTER_ID
        assert meta.kind == kind
        assert meta.synthetic is True
        assert meta.storage_variant is not None


def test_factory_identity_and_beta_stability() -> None:
    adapter = create_adapter()
    assert adapter.adapter_id == ADAPTER_ID == "cursor"
    assert adapter.adapter_version == ADAPTER_VERSION
    assert adapter.stability is Stability.BETA


def test_discover_success_inventories_g4_store(tmp_path: Path) -> None:
    _adapter, outcome, home = _discover("success", tmp_path)
    record = _single_record(outcome)

    assert record.opaque_label == "Cursor 1"
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.stability is Stability.BETA
    assert record.storage_format == "sqlite"
    assert record.schema_fingerprint == "g4+g5;composer_v=10-16;bubble_v=3"
    assert record.estimated_records == 4
    assert record.candidate_messages == len(SUCCESS_TEXTS)
    assert record.candidate_words == sum(count_words(text) for text in SUCCESS_TEXTS)
    assert record.candidate_bytes == sum(len(text.encode("utf-8")) for text in SUCCESS_TEXTS)
    assert record.earliest_timestamp == datetime(2025, 5, 4, 9, 30, tzinfo=UTC)
    assert record.latest_timestamp == datetime(2025, 6, 15, 18, 45, tzinfo=UTC)

    root = home / USER_RELATIVE
    expected_hash = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    assert record.path_hash == expected_hash
    assert record.instance_key == expected_hash
    assert outcome.instance_paths[record.instance_key] == root


def test_discover_success_exclusion_counters(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("success", tmp_path)
    record = _single_record(outcome)
    stats = adapter.inventory_stats(record.instance_key)
    assert stats is not None
    scan = stats.scan
    assert scan is not None

    assert scan.composers_total == 4
    assert scan.composers_g4 == 4
    assert scan.composers_g4_eligible == 2
    assert scan.excluded_best_of_n == 1
    assert scan.excluded_sub_composer == 1
    assert scan.bubbles_referenced == 12
    assert scan.bubbles_fetched == 12
    assert scan.bubbles_missing == 0
    assert scan.bubbles_kept == 8
    assert scan.excluded_nudge == 1
    assert scan.excluded_quick_search == 1
    assert scan.excluded_skip_rendering == 1
    assert scan.excluded_empty == 1
    assert scan.wrapper_leaks == 0
    assert scan.fidelity_checked == 8
    assert scan.fidelity_mismatches == 0
    assert scan.composer_versions == {10, 12, 13, 16}
    assert scan.bubble_versions == {3}

    assert stats.workspaces_indexed == 1
    assert stats.workspace_composer_links == 1
    folder_hash = hashlib.sha256(WORKSPACE_FOLDER.encode("utf-8")).hexdigest()
    assert stats.workspace_folder_hashes == (folder_hash,)
    assert stats.legacy_g1_workspaces == 0
    assert stats.legacy_g2_workspaces == 0
    assert stats.g5_store_detected is True
    assert stats.g5_chat_sessions == 1
    assert stats.cursor_server_detected is False


def test_discover_not_found_and_empty(tmp_path: Path) -> None:
    adapter = CursorAdapter()
    missing = _single_record(adapter.discover(_context(tmp_path / "nobody-home")))
    assert missing.accessibility is Accessibility.NOT_FOUND
    assert missing.diagnostic_code == "SOURCE_NOT_FOUND"
    assert missing.schema_fingerprint == "absent"
    assert missing.candidate_messages == 0

    home = _build_home("empty", tmp_path / "empty")
    empty = _single_record(CursorAdapter().discover(_context(home)))
    assert empty.accessibility is Accessibility.FOUND
    assert empty.diagnostic_code is None
    assert empty.schema_fingerprint == "empty"
    assert empty.estimated_records == 0
    assert empty.candidate_messages == 0
    assert empty.candidate_words == 0
    assert empty.earliest_timestamp is None


def test_discover_not_a_database_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "home" / USER_RELATIVE
    (root / "globalStorage").mkdir(parents=True)
    (root / "globalStorage" / "state.vscdb").write_bytes(b"not a database at all")
    record = _single_record(CursorAdapter().discover(_context(tmp_path / "home")))
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.candidate_messages == 0


def test_discover_malformed_thresholds(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("malformed", tmp_path)
    record = _single_record(outcome)
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 16
    assert record.estimated_records == 3
    assert record.schema_fingerprint == "g4;composer_v=10-14;bubble_v=3"
    assert record.earliest_timestamp == datetime(2025, 8, 1, 10, 0, tzinfo=UTC)
    assert record.latest_timestamp == datetime(2025, 8, 1, 10, 15, tzinfo=UTC)

    stats = adapter.inventory_stats(record.instance_key)
    assert stats is not None and stats.scan is not None
    scan = stats.scan
    assert scan.composers_total == 3
    assert scan.composers_malformed == 1
    assert scan.composers_missing_bubbles == 1
    assert scan.composers_g4_eligible == 1
    assert scan.bubbles_referenced == 24
    assert scan.bubbles_fetched == 21
    assert scan.bubbles_missing == 3
    assert scan.bubbles_malformed == 2
    assert scan.bubbles_kept == 16
    assert not scan.malformed_bubbles_over_threshold


def test_discover_unsupported_versions_fail_closed(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("unsupported", tmp_path)
    record = _single_record(outcome)
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "g4;composer_v=1-12;bubble_v=99"
    assert record.estimated_records == 2
    assert record.candidate_messages == 0
    assert record.candidate_words == 0
    assert record.earliest_timestamp is None

    stats = adapter.inventory_stats(record.instance_key)
    assert stats is not None and stats.scan is not None
    assert stats.scan.composers_unsupported_version == 1
    assert stats.scan.bubbles_unsupported_version == 3
    assert stats.scan.unsupported_bubbles_dominate


def test_discover_migration_legacy_inventoried_without_text(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("migration", tmp_path)
    record = _single_record(outcome)
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "g1+g2+g3"
    assert record.estimated_records == 1
    assert record.candidate_messages == 0
    assert record.candidate_bytes == 0

    stats = adapter.inventory_stats(record.instance_key)
    assert stats is not None and stats.scan is not None
    assert stats.scan.composers_g3 == 1
    assert stats.scan.composers_g4 == 0
    assert stats.legacy_g1_workspaces == 1
    assert stats.legacy_g2_workspaces == 1
    assert stats.workspaces_indexed == 1
    assert stats.workspace_composer_links == 1


def test_windows_and_linux_root_resolution(tmp_path: Path) -> None:
    global_sql = FIXTURES / "success" / "home" / USER_RELATIVE / "globalStorage" / "state.vscdb.sql"

    linux_home = tmp_path / "linux-home"
    linux_db = linux_home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    linux_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(linux_db)
    connection.executescript(global_sql.read_text(encoding="utf-8"))
    connection.close()
    linux = _single_record(
        CursorAdapter().discover(_context(linux_home, os_environment=OsEnvironment.LINUX))
    )
    assert linux.accessibility is Accessibility.FOUND
    assert linux.candidate_messages == len(SUCCESS_TEXTS)

    windows_home = tmp_path / "windows-home"
    appdata = windows_home / "AppData" / "Roaming"
    windows_db = appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    windows_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(windows_db)
    connection.executescript(global_sql.read_text(encoding="utf-8"))
    connection.close()
    windows = _single_record(
        CursorAdapter().discover(
            _context(
                windows_home,
                environ={"APPDATA": str(appdata)},
                os_environment=OsEnvironment.WINDOWS,
            )
        )
    )
    assert windows.accessibility is Accessibility.FOUND
    assert windows.candidate_messages == len(SUCCESS_TEXTS)


def test_wsl_fails_closed_with_host_store_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cursor_adapter, "_WSL_MOUNT_BASE", tmp_path / "mnt")

    without = _single_record(
        CursorAdapter().discover(_context(tmp_path / "wsl-home", os_environment=OsEnvironment.WSL))
    )
    assert without.accessibility is Accessibility.NOT_FOUND
    assert without.diagnostic_code == "SOURCE_NOT_FOUND"

    host_store = (
        tmp_path
        / "mnt"
        / "c"
        / "Users"
        / "fake-user"
        / "AppData"
        / "Roaming"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )
    host_store.parent.mkdir(parents=True)
    host_store.write_bytes(b"")
    hinted = _single_record(
        CursorAdapter().discover(_context(tmp_path / "wsl-home", os_environment=OsEnvironment.WSL))
    )
    assert hinted.accessibility is Accessibility.NOT_FOUND
    assert hinted.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT"
    assert hinted.candidate_messages == 0


def test_snapshot_manifest_is_complete_and_verifiable(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("success", tmp_path)
    record = _single_record(outcome)
    source = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source, target)

    listed = sorted(entry.relative_path for entry in capture.files)
    on_disk = sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    )
    assert listed == on_disk
    assert "globalStorage/state.vscdb" in listed
    assert f"workspaceStorage/{WORKSPACE_DIR_NAME}/state.vscdb" in listed
    assert "cursor-snapshot-meta.json" in listed
    assert not any(name.endswith(".backup") for name in listed)

    for entry in capture.files:
        data = (target / entry.relative_path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry.sha256
        assert len(data) == entry.size_bytes

    mode = (target / "globalStorage" / "state.vscdb").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600

    meta_raw = (target / "cursor-snapshot-meta.json").read_text(encoding="utf-8")
    assert "fake-webapp" not in meta_raw
    assert "synthetic-user" not in meta_raw
    meta = json.loads(meta_raw)
    assert meta["reason"] == BETA_NO_TEXT_REASON
    folder_hash = hashlib.sha256(WORKSPACE_FOLDER.encode("utf-8")).hexdigest()
    assert meta["workspace_folder_hashes"] == {WORKSPACE_DIR_NAME: folder_hash}


def test_snapshot_backs_up_live_wal_database(tmp_path: Path) -> None:
    home = _build_home("empty", tmp_path / "wal")
    root = home / USER_RELATIVE
    database_path = root / "globalStorage" / "state.vscdb"

    composer_id = "77777777-1111-4111-8111-777777777777"
    bubble_id = "77770001-1111-4111-8111-000000000001"
    composer = {
        "composerId": composer_id,
        "_v": 12,
        "createdAt": 1754038800000,
        "conversationMap": {},
        "fullConversationHeadersOnly": [{"bubbleId": bubble_id, "type": 1}],
    }
    bubble = {
        "_v": 3,
        "type": 1,
        "text": "This message live only inside the WAL for now.",
        "createdAt": "2025-08-01T10:00:00.000Z",
    }
    live = sqlite3.connect(database_path)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer)),
        )
        live.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            (f"bubbleId:{composer_id}:{bubble_id}", json.dumps(bubble)),
        )
        live.commit()
        assert database_path.with_name("state.vscdb-wal").is_file()

        adapter = CursorAdapter()
        outcome = adapter.discover(_context(home))
        record = _single_record(outcome)
        assert record.candidate_messages == 1

        target = tmp_path / "wal-snap"
        adapter.snapshot(record, root, target)
    finally:
        live.close()

    assert list(adapter.extract(record, target)) == []
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None and stats.scan is not None
    assert stats.scan.candidate_messages == 1
    assert adapter.verify(record, []) == []


def test_snapshot_insufficient_space_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, outcome, _ = _discover("success", tmp_path)
    record = _single_record(outcome)
    source = outcome.instance_paths[record.instance_key]
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=990, free=10),
    )
    with pytest.raises(OSError, match="insufficient free space"):
        adapter.snapshot(record, source, tmp_path / "no-space")
    assert not (tmp_path / "no-space" / "globalStorage" / "state.vscdb").exists()


def test_extract_yields_nothing_and_records_reason(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("success", tmp_path)
    record = _single_record(outcome)
    source = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    adapter.snapshot(record, source, target)

    extracted = list(adapter.extract(record, target))
    assert extracted == []

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.reason == BETA_NO_TEXT_REASON
    assert stats.scan is not None
    assert stats.scan.candidate_messages == record.candidate_messages
    assert stats.scan.candidate_words == record.candidate_words
    assert adapter.verify(record, extracted) == []


def test_verify_rejects_any_contributed_text(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("success", tmp_path)
    record = _single_record(outcome)
    smuggled = NormalizedUtterance(
        utterance_id=f"{ADAPTER_ID}-0000000000000000-fake",
        source_adapter=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        session_hash="0" * 64,
        timestamp=None,
        text="This text must never leave the beta adapter.",
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role_type1",
        source_path_hash="0" * 64,
    )
    codes = {diagnostic.code for diagnostic in adapter.verify(record, [smuggled])}
    assert "SCHEMA_INVALID_VALUE" in codes


def test_extract_from_unsupported_snapshot_stays_empty(tmp_path: Path) -> None:
    adapter, outcome, _ = _discover("unsupported", tmp_path)
    record = _single_record(outcome)
    source = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    adapter.snapshot(record, source, target)
    assert list(adapter.extract(record, target)) == []
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None and stats.scan is not None
    assert stats.scan.candidate_messages == 0
    assert adapter.verify(record, []) == []


def _connect_target(argument: object) -> str:
    text = str(argument)
    if text.startswith("file:"):
        text = text[len("file:") :].split("?", 1)[0]
        text = urllib.parse.unquote(text)
    return text


def test_never_opens_denylisted_files_or_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _build_home("success", tmp_path / "cred")
    root = home / USER_RELATIVE

    opened: list[Path] = []
    connected: list[str] = []
    original_open = Path.open
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_connect = sqlite3.connect

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return original_open(self, *args, **kwargs)

    def recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self)
        return original_read_text(self, *args, **kwargs)

    def recording_read_bytes(self: Path) -> bytes:
        opened.append(self)
        return original_read_bytes(self)

    def recording_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        connected.append(_connect_target(database))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    adapter = CursorAdapter()
    outcome = adapter.discover(_context(home))
    record = _single_record(outcome)
    target = tmp_path / "snap"
    adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    extracted = list(adapter.extract(record, target))
    assert adapter.verify(record, extracted) == []

    assert opened, "expected file reads to be recorded"
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not any(part in DENY_DIR_NAMES for part in path.parts), str(path)

    assert connected, "expected sqlite connections to be recorded"
    allowed_source_targets = {
        str((root / "globalStorage" / "state.vscdb").resolve()),
        str((root / "workspaceStorage" / WORKSPACE_DIR_NAME / "state.vscdb").resolve()),
    }
    for target_text in connected:
        resolved = str(Path(target_text).resolve())
        if resolved.startswith(str(home.resolve())):
            assert resolved in allowed_source_targets, target_text
        assert not resolved.endswith(".backup"), target_text
        assert not resolved.endswith("store.db"), target_text

    audit = adapter.opened_key_audit()
    assert audit, "expected database key accesses to be recorded"
    for kind, key in audit:
        assert "cursorAuth" not in key and "secret://" not in key, (kind, key)
        if kind in {"kv_range", "kv_get"}:
            assert key.startswith(("composerData:", "bubbleId:")), (kind, key)
        elif kind == "item_get":
            assert key in {"composer.composerHeaders", "composer.composerData"}, (kind, key)
        elif kind == "item_present":
            assert key == "workbench.panel.aichat.view.aichat.chatdata", (kind, key)
        else:
            pytest.fail(f"unexpected audit kind: {kind}")

    for path in adapter.opened_paths():
        assert path.name in {"state.vscdb", "workspace.json"}, str(path)
