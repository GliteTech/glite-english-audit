"""Fixture-driven tests for the Wispr Flow source adapter."""

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from glite_english_audit.adapters.wispr_flow import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ALLOWED_SOURCE_NAMES,
    NEVER_OPEN_DIR_NAMES,
    NEVER_OPEN_NAMES,
    WisprFlowAdapter,
    create_adapter,
)
from glite_english_audit.adapters.wispr_flow import (
    adapter as wispr_adapter,
)
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
FIXTURES = REPO_ROOT / "fixtures" / "wispr_flow"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

SUCCESS_TEXTS = {
    "I very like this plan, let us start from the first step.",
    "Please explain me how this function works, I am not understanding it.",
    "Yesterday I have wrote the report and sended it to my colleague.",
    "How I can improve my English speaking without a teacher?",
    "I am agree with the last comment, we should discuss it tomorrow.",
    "Make this text more shorter and more polite, please.",
    "This are my notes from the morning stand up meeting.",
    "I want to say thank you for helping me with this difficult task.",
    "Since two years I am using this dictation application every day.",
    "The weather today is very nice, we can to go outside for lunch.",
}
MIGRATION_TEXTS = {
    "Can you check my text for mistakes, I wrote it very quick.",
    "I am not sure how to say this correct in English.",
    "We discussed about the project and decided to continue next week.",
}
SUCCESS_FINGERPRINT = "history-2024/opt8of8/mig12/extra5+1"

_ALLOWLIST_DDL = (
    'CREATE TABLE "History" ('
    '"transcriptEntityId" TEXT, "asrText" TEXT, "timestamp" TEXT, "app" TEXT, '
    '"language" TEXT, "conversationId" TEXT, "status" TEXT, "isArchived" INTEGER, '
    '"appVersion" TEXT, "numWords" INTEGER, "duration" REAL)'
)


def _context(
    home: Path,
    environ: dict[str, str] | None = None,
    os_environment: OsEnvironment = OsEnvironment.MACOS,
) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=os_environment, home=home, now=NOW, environ=environ or {}
    )


def _session_hash(basis: str) -> str:
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _data_root(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Wispr Flow"


def _row(
    tid: object,
    text: object,
    timestamp: object,
    *,
    status: str | None = "formatted",
) -> tuple[object, ...]:
    return (tid, text, timestamp, None, "en", None, status, 0, "1.5.308", None, None)


def _make_store(db: Path, rows: list[tuple[object, ...]]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db))
    connection.execute(_ALLOWLIST_DDL)
    connection.executemany('INSERT INTO "History" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    connection.commit()
    connection.close()


def _snapshot_and_extract(
    adapter: WisprFlowAdapter,
    outcome: DiscoveryOutcome,
    record: SourceInstanceRecord,
    target: Path,
) -> list[NormalizedUtterance]:
    adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    return list(adapter.extract(record, target))


def test_fixture_meta_declarations() -> None:
    expected = {
        "success": "success",
        "empty": "empty",
        "malformed": "malformed",
        "unsupported": "unsupported",
        "migration": "migration",
    }
    for variant, kind in expected.items():
        meta = load_fixture_meta(FIXTURES / variant)
        assert meta.adapter_id == ADAPTER_ID
        assert meta.kind == kind
        assert meta.synthetic is True
        assert meta.storage_variant is not None
    builder_meta = load_fixture_meta(FIXTURES)
    assert builder_meta.kind == "unit"
    assert builder_meta.synthetic is True


def test_factory_and_identity() -> None:
    adapter = create_adapter()
    assert adapter.adapter_id == ADAPTER_ID == "wispr_flow"
    assert adapter.adapter_version == ADAPTER_VERSION
    # Beta until the spec section 9 real-installation smoke tests pass.
    assert adapter.stability is Stability.BETA


def test_discover_success_single_instance() -> None:
    adapter = WisprFlowAdapter()
    home = FIXTURES / "success" / "home"
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 1
    record = outcome.records[0]

    assert record.opaque_label == "Wispr Flow 1"
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.storage_format == "sqlite"
    assert record.stability is Stability.BETA
    assert record.schema_fingerprint == SUCCESS_FINGERPRINT
    assert record.app_version == "1.5.308"
    assert record.candidate_messages == len(SUCCESS_TEXTS)
    assert record.estimated_records == 12
    assert record.candidate_words == sum(count_words(text) for text in SUCCESS_TEXTS)
    assert record.candidate_bytes == sum(len(text.encode("utf-8")) for text in SUCCESS_TEXTS)
    assert record.earliest_timestamp == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert record.latest_timestamp == datetime(2026, 6, 2, 8, 0, tzinfo=UTC)

    root = _data_root(home)
    expected_hash = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    assert record.path_hash == expected_hash
    assert record.instance_key == expected_hash
    assert outcome.instance_paths[record.instance_key] == root


def test_discover_records_carry_no_source_text() -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    dumped = json.dumps([record.model_dump(mode="json") for record in outcome.records])
    for text in SUCCESS_TEXTS:
        assert text not in dumped
    assert "SENTINEL" not in dumped
    assert "conv-alpha" not in dumped
    assert "sk-FAKE" not in dumped


def test_discover_not_found(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()

    missing_home = adapter.discover(_context(tmp_path / "nobody"))
    assert len(missing_home.records) == 1
    assert missing_home.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing_home.records[0].diagnostic_code == "SOURCE_NOT_FOUND"
    assert missing_home.records[0].candidate_messages == 0

    # A data root without flow.sqlite is equally not found (spec 6.1).
    _data_root(tmp_path / "installed").mkdir(parents=True)
    rootless = adapter.discover(_context(tmp_path / "installed"))
    assert rootless.records[0].accessibility is Accessibility.NOT_FOUND


def test_discover_empty_fixture() -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.candidate_messages == 0
    assert record.candidate_words == 0
    assert record.estimated_records == 2
    assert record.earliest_timestamp is None
    assert record.schema_fingerprint == "history-2024/opt8of8/mig12/extra0+0"
    assert record.app_version is None


def test_discover_malformed_not_sqlite() -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "not-sqlite"
    assert record.candidate_messages == 0
    assert record.estimated_records == 0


def test_discover_corrupt_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _data_root(home) / "flow.sqlite"
    rows = [
        _row(
            f"eeeeeeee-{i:04d}-4eee-8eee-{i:012d}",
            f"Synthetic sentence number {i} that fills the page with some extra words.",
            f"2026-07-01 {10 + i // 60:02d}:{i % 60:02d}:00.000 +00:00",
        )
        for i in range(200)
    ]
    _make_store(db, rows)
    data = db.read_bytes()
    db.write_bytes(data[: len(data) // 2])

    adapter = WisprFlowAdapter()
    record = adapter.discover(_context(home)).records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "corrupt"
    assert record.candidate_messages == 0


def test_discover_unsupported_fails_closed(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "unsupported" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "unsupported:missing-required"
    assert record.candidate_messages == 0
    assert record.candidate_words == 0

    extracted = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")
    assert extracted == []
    dumped = json.dumps([record.model_dump(mode="json")])
    assert "SENTINEL" not in dumped


def test_discover_migration_reduced_generation(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "history-2024/opt2of8/mig2/extra1+0"
    assert record.candidate_messages == 3
    assert record.estimated_records == 3
    assert record.app_version is None
    assert record.earliest_timestamp == datetime(2024, 6, 10, 9, 0, tzinfo=UTC)
    assert record.latest_timestamp == datetime(2024, 6, 11, 8, 0, tzinfo=UTC)

    extracted = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")
    assert {utterance.text for utterance in extracted} == MIGRATION_TEXTS
    # No conversationId column: every dictation is its own session.
    assert len({utterance.session_hash for utterance in extracted}) == 3
    by_text = {utterance.text: utterance for utterance in extracted}
    windows_app = by_text["Can you check my text for mistakes, I wrote it very quick."]
    assert windows_app.destination_app == "other"
    assert adapter.verify(record, extracted) == []


def test_windows_appdata_resolution(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    roaming = FIXTURES / "success" / "home" / "Library" / "Application Support"
    outcome = adapter.discover(
        _context(
            tmp_path,
            environ={"APPDATA": str(roaming)},
            os_environment=OsEnvironment.WINDOWS,
        )
    )
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == len(SUCCESS_TEXTS)
    assert record.os_environment is OsEnvironment.WINDOWS

    without_appdata = adapter.discover(_context(tmp_path, os_environment=OsEnvironment.WINDOWS))
    assert without_appdata.records[0].accessibility is Accessibility.NOT_FOUND


def test_wsl_fails_closed_without_opening_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "mnt"
    host_root = mount / "c" / "Users" / "fake-user" / "AppData" / "Roaming" / "Wispr Flow"
    host_root.mkdir(parents=True)
    host_db = host_root / "flow.sqlite"
    host_db.write_bytes(b"FAKE WINDOWS HOST STORE - MUST NEVER BE OPENED")
    monkeypatch.setattr(wispr_adapter, "_WSL_MOUNT_BASE", mount)

    opened: list[Path] = []
    real_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(tmp_path, os_environment=OsEnvironment.WSL))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.INACCESSIBLE
    assert record.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT"
    assert record.schema_fingerprint == "wsl-host-store"
    assert record.candidate_messages == 0
    assert record.estimated_records == 0
    assert outcome.instance_paths[record.instance_key] == host_root
    assert not any(path == host_db for path in opened)

    empty_mount = tmp_path / "mnt-empty"
    empty_mount.mkdir()
    monkeypatch.setattr(wispr_adapter, "_WSL_MOUNT_BASE", empty_mount)
    nothing = adapter.discover(_context(tmp_path, os_environment=OsEnvironment.WSL))
    assert nothing.records[0].accessibility is Accessibility.NOT_FOUND


def test_linux_is_never_probed() -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(
        _context(FIXTURES / "success" / "home", os_environment=OsEnvironment.LINUX)
    )
    record = outcome.records[0]
    assert record.accessibility is Accessibility.NOT_FOUND
    assert record.diagnostic_code == "SOURCE_NOT_FOUND"
    assert adapter.extraction_stats(record.instance_key) is None


def test_snapshot_manifest_is_complete(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    home = FIXTURES / "success" / "home"
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    source_db = _data_root(home) / "flow.sqlite"
    source_bytes = source_db.read_bytes()

    target = tmp_path / "snap"
    capture = adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    assert capture.snapshot_relative_dir == f"wispr_flow/{record.instance_key[:16]}"

    on_disk = sorted(path.name for path in target.rglob("*") if path.is_file())
    assert on_disk == sorted(entry.relative_path for entry in capture.files)
    for entry in capture.files:
        copied = target / entry.relative_path
        payload = copied.read_bytes()
        assert entry.size_bytes == len(payload)
        assert entry.sha256 == hashlib.sha256(payload).hexdigest()
        if os.name == "posix":
            assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    # The live store is untouched, and the copy is a consistent database.
    assert source_db.read_bytes() == source_bytes
    meta = json.loads((target / "wispr-flow-snapshot-meta.json").read_text(encoding="utf-8"))
    assert meta["journal_mode"] == "delete"
    assert (
        meta["source_path_hash"]
        == hashlib.sha256(str(source_db.resolve()).encode("utf-8")).hexdigest()
    )


def test_extract_success_golden(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    home = FIXTURES / "success" / "home"
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    utterances = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")

    assert {utterance.text for utterance in utterances} == SUCCESS_TEXTS
    assert len(utterances) == record.candidate_messages
    assert len({utterance.utterance_id for utterance in utterances}) == len(utterances)

    source_db = _data_root(home) / "flow.sqlite"
    source_hash = hashlib.sha256(str(source_db.resolve()).encode("utf-8")).hexdigest()
    for utterance in utterances:
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.modality is Modality.SPOKEN_ASR
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.authorship_basis == "sole_dictation_field"
        assert utterance.authorship_confidence == pytest.approx(0.95)
        assert utterance.source_path_hash == source_hash
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")

    by_text = {utterance.text: utterance for utterance in utterances}
    first = by_text["I very like this plan, let us start from the first step."]
    second = by_text["Please explain me how this function works, I am not understanding it."]
    conversation_hash = _session_hash("wispr_flow|conversation|conv-alpha")
    assert first.session_hash == conversation_hash
    assert second.session_hash == conversation_hash
    assert first.utterance_id == (
        f"{ADAPTER_ID}-{conversation_hash[:16]}-aaaaaaaa-0001-4aaa-8aaa-000000000001"
    )
    assert first.timestamp == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert first.destination_app == "code_editor"
    assert first.content_flags == []

    solo = by_text["Yesterday I have wrote the report and sended it to my colleague."]
    assert solo.session_hash == _session_hash(
        "wispr_flow|transcript|aaaaaaaa-0003-4aaa-8aaa-000000000003"
    )
    assert solo.destination_app == "email"

    dismissed = by_text["I am agree with the last comment, we should discuss it tomorrow."]
    assert dismissed.content_flags == ["dismissed"]
    assert dismissed.destination_app is None

    command = by_text["Make this text more shorter and more polite, please."]
    assert command.content_flags == ["command_mode"]
    assert command.destination_app == "browser"

    archived = by_text["This are my notes from the morning stand up meeting."]
    assert archived.content_flags == ["archived"]
    assert archived.destination_app == "messaging"

    offset = by_text["I want to say thank you for helping me with this difficult task."]
    assert offset.timestamp == datetime(2026, 6, 2, 7, 30, tzinfo=UTC)
    assert offset.destination_app == "notes"

    undated = by_text["Since two years I am using this dictation application every day."]
    assert undated.timestamp is None
    assert undated.content_flags == ["undated"]
    assert undated.destination_app == "terminal"

    unknown_status = by_text["The weather today is very nice, we can to go outside for lunch."]
    assert unknown_status.content_flags == []
    assert unknown_status.timestamp == datetime(2026, 6, 2, 8, 0, tzinfo=UTC)

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.utterance_count == 10
    assert stats.total_rows == 12
    assert stats.empty_asr_rows == 2
    assert stats.pk_anomalies == 0
    assert stats.unknown_status_rows == 1
    assert stats.wordcount_divergent_rows == 1

    # No sentinel from any denylisted column or table ever surfaces.
    dumped = json.dumps([utterance.model_dump(mode="json") for utterance in utterances])
    assert "SENTINEL" not in dumped
    assert "sk-FAKE" not in dumped
    assert "example.invalid" not in dumped

    again = list(adapter.extract(record, tmp_path / "snap"))
    assert again == utterances
    assert adapter.verify(record, utterances) == []


def test_wal_store_snapshot_captures_uncheckpointed_rows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _data_root(home) / "flow.sqlite"
    db.parent.mkdir(parents=True)
    writer = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(_ALLOWLIST_DDL)
        writer.execute(
            'INSERT INTO "History" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            _row(
                "ffffffff-0001-4fff-8fff-000000000001",
                "The first sentence was written before the checkpoint.",
                "2026-07-01 10:00:00.000 +00:00",
            ),
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.executemany(
            'INSERT INTO "History" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                _row(
                    "ffffffff-0002-4fff-8fff-000000000002",
                    "This sentence live only inside the write ahead log.",
                    "2026-07-01 10:05:00.000 +00:00",
                ),
                _row(
                    "ffffffff-0003-4fff-8fff-000000000003",
                    "Also this one is not yet checkpointed to the main file.",
                    "2026-07-01 10:10:00.000 +00:00",
                ),
            ],
        )
        writer.commit()
        wal = db.parent / "flow.sqlite-wal"
        assert wal.is_file() and wal.stat().st_size > 0
        main_bytes = db.read_bytes()
        wal_bytes = wal.read_bytes()

        adapter = WisprFlowAdapter()
        outcome = adapter.discover(_context(home))
        record = outcome.records[0]
        assert record.accessibility is Accessibility.FOUND
        assert record.candidate_messages == 3

        target = tmp_path / "snap"
        capture = adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
        utterances = list(adapter.extract(record, target))
        assert {utterance.text for utterance in utterances} == {
            "The first sentence was written before the checkpoint.",
            "This sentence live only inside the write ahead log.",
            "Also this one is not yet checkpointed to the main file.",
        }
        meta_entry = json.loads(
            (target / "wispr-flow-snapshot-meta.json").read_text(encoding="utf-8")
        )
        assert meta_entry["journal_mode"] == "wal"
        assert capture.files, "snapshot must list its files"

        # The live store is bit-identical after snapshotting.
        assert db.read_bytes() == main_bytes
        assert wal.read_bytes() == wal_bytes
        assert adapter.verify(record, utterances) == []
    finally:
        writer.close()


def test_fallback_copy_when_backup_api_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    db = _data_root(home) / "flow.sqlite"
    db.parent.mkdir(parents=True)
    writer = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(_ALLOWLIST_DDL)
        writer.execute(
            'INSERT INTO "History" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            _row(
                "ffffffff-0011-4fff-8fff-000000000011",
                "This sentence must survive the fallback copy path.",
                "2026-07-02 09:00:00.000 +00:00",
            ),
        )
        writer.commit()

        adapter = WisprFlowAdapter()
        outcome = adapter.discover(_context(home))
        record = outcome.records[0]

        def failing_backup(self: WisprFlowAdapter, source_db: Path, target_db: Path) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(WisprFlowAdapter, "_backup", failing_backup)
        target = tmp_path / "snap"
        capture = adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
        on_disk = sorted(path.name for path in target.rglob("*") if path.is_file())
        assert on_disk == sorted(entry.relative_path for entry in capture.files)

        utterances = list(adapter.extract(record, target))
        assert [utterance.text for utterance in utterances] == [
            "This sentence must survive the fallback copy path."
        ]
    finally:
        writer.close()


def test_pk_anomalies_below_threshold_are_excluded(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _data_root(home) / "flow.sqlite"
    rows = [
        _row(
            f"dddddddd-{i:04d}-4ddd-8ddd-{i:012d}",
            f"This is the synthetic learner sentence number {i} for the threshold test.",
            f"2026-07-01 {10 + i // 60:02d}:{i % 60:02d}:00.000 +00:00",
        )
        for i in range(300)
    ]
    rows.append(
        _row(
            "not-a-uuid-but-unique",
            "This sentence keep its row even with a strange identifier.",
            "2026-07-01 16:00:00.000 +00:00",
        )
    )
    rows.append(
        _row(
            "dddddddd-9999-4ddd-8ddd-999999999999",
            "The duplicated key makes this row anomalous.",
            "2026-07-01 16:05:00.000 +00:00",
        )
    )
    rows.append(
        _row(
            "dddddddd-9999-4ddd-8ddd-999999999999",
            "The duplicated key makes this second row anomalous too.",
            "2026-07-01 16:06:00.000 +00:00",
        )
    )
    rows.append(
        _row(
            None,
            "The missing key makes this row anomalous.",
            "2026-07-01 16:07:00.000 +00:00",
        )
    )
    _make_store(db, rows)

    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 301
    assert record.estimated_records == 304

    utterances = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")
    assert len(utterances) == 301
    texts = {utterance.text for utterance in utterances}
    assert "The duplicated key makes this row anomalous." not in texts
    assert "The missing key makes this row anomalous." not in texts
    flagged = [u for u in utterances if "nonuuid_pk" in u.content_flags]
    assert len(flagged) == 1
    assert flagged[0].text == "This sentence keep its row even with a strange identifier."

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.pk_anomalies == 3
    assert adapter.verify(record, utterances) == []


def test_pk_anomalies_above_threshold_fail_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _data_root(home) / "flow.sqlite"
    rows = [
        _row(
            f"dddddddd-{i:04d}-4ddd-8ddd-{i:012d}",
            f"This is the synthetic sentence number {i} for the failing store.",
            f"2026-07-01 10:{i:02d}:00.000 +00:00",
        )
        for i in range(10)
    ]
    rows.append(
        _row(
            "dddddddd-9999-4ddd-8ddd-999999999999",
            "The duplicated key appears here first.",
            "2026-07-01 11:00:00.000 +00:00",
        )
    )
    rows.append(
        _row(
            "dddddddd-9999-4ddd-8ddd-999999999999",
            "The duplicated key appears here again.",
            "2026-07-01 11:01:00.000 +00:00",
        )
    )
    _make_store(db, rows)

    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "unsupported:pk-anomalies"
    assert record.candidate_messages == 0

    extracted = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")
    assert extracted == []


def test_verify_clean_and_broken(tmp_path: Path) -> None:
    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = outcome.records[0]
    utterances = _snapshot_and_extract(adapter, outcome, record, tmp_path / "snap")
    assert adapter.verify(record, utterances) == []

    duplicated = adapter.verify(record, [*utterances, utterances[0]])
    assert any(diagnostic.code == "CARDINALITY_MISMATCH" for diagnostic in duplicated)

    blank = utterances[0].model_copy(update={"text": "   "})
    blank_diagnostics = adapter.verify(record, [blank, *utterances[1:]])
    assert any(diagnostic.code == "SCHEMA_INVALID_VALUE" for diagnostic in blank_diagnostics)

    naive = utterances[0].model_copy(update={"timestamp": datetime(2026, 6, 1, 10, 0)})
    naive_diagnostics = adapter.verify(record, [naive, *utterances[1:]])
    assert any(diagnostic.code == "SCHEMA_INVALID_VALUE" for diagnostic in naive_diagnostics)

    outside_range = utterances[0].model_copy(
        update={"timestamp": datetime(2025, 6, 1, 10, 0, tzinfo=UTC)}
    )
    range_diagnostics = adapter.verify(record, [outside_range, *utterances[1:]])
    assert any(
        diagnostic.code == "SCHEMA_INVALID_VALUE" and "range" in diagnostic.message
        for diagnostic in range_diagnostics
    )

    short = adapter.verify(record, utterances[:-1])
    assert any(
        diagnostic.code == "CARDINALITY_MISMATCH" and "discovery counted" in diagnostic.message
        for diagnostic in short
    )


def test_never_opens_denylisted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = FIXTURES / "success" / "home"
    accessed: list[Path] = []
    real_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    real_connect = sqlite3.connect

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        accessed.append(self)
        return real_open(self, *args, **kwargs)

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        accessed.append(self)
        return real_read_text(self, *args, **kwargs)

    def spy_read_bytes(self: Path) -> bytes:
        accessed.append(self)
        return real_read_bytes(self)

    def spy_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(database, str) and database.startswith("file:"):
            accessed.append(Path(unquote(urlparse(database).path)))
        elif isinstance(database, str | Path):
            accessed.append(Path(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    adapter = WisprFlowAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    target = tmp_path / "snap"
    adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    utterances = list(adapter.extract(record, target))
    assert adapter.verify(record, utterances) == []

    assert accessed, "the spy must have recorded adapter file access"
    resolved_home = home.resolve()
    touched_in_home = [
        path
        for path in accessed
        if path.is_absolute() and path.resolve().is_relative_to(resolved_home)
    ]
    assert touched_in_home, "the fixture store itself must have been read"
    for path in touched_in_home:
        assert path.name in ALLOWED_SOURCE_NAMES, str(path)
        assert path.name not in NEVER_OPEN_NAMES, str(path)
        assert not path.name.startswith("backup-"), str(path)
        assert not any(part in NEVER_OPEN_DIR_NAMES for part in path.parts), str(path)
    accessed_names = {path.name for path in accessed}
    assert "config.json" not in accessed_names
    assert "SharedStorage" not in accessed_names
    assert "backup-2026-01-01T00-00-00.000Z.sqlite" not in accessed_names
    assert "000001.log" not in accessed_names
    assert "main.log" not in accessed_names
    assert "accessibility.log" not in accessed_names
