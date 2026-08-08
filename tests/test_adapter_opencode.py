"""Fixture-driven tests for the OpenCode source adapter."""

import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.opencode import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    OpenCodeAdapter,
    create_adapter,
)
from glite_english_audit.adapters.opencode.adapter import DENY_DIR_NAMES, DENY_FILE_NAMES
from glite_english_audit.adapters.opencode.stores import VENDOR_TEMPLATE_TEXTS
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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"

ALPHA_TEXTS = {
    "I very like this plan, can we start from the login page?",
    "Here is my plan for the next sprint.\n\n"
    "Please review it and tell me if something is wrong there.",
    "Also the tests are still red, why it can be?",
    "Please explain me how the cache invalidation works here.",
    "This task is finish, I will close the session now.",
}
BETA_TEXTS = {
    "Today I written a short note about my English learning progress.",
    "Can you help me to make this sentence more natural? It sounds not good for me.",
    "I am agree with your suggestion, let us do it so.",
    "Since many years I want to speak English more fluent.",
    "How I can practice the passive voice more often?",
}
MIGRATION_M01 = "I have doubt about how to name this function, can you propose something?"
MIGRATION_M02 = "This code work good on my machine but fails on the server, why so?"


def _materialize(variant: str, destination: Path) -> Path:
    """Copy the fixture home tree and build its databases from committed SQL."""
    source = FIXTURES / variant
    home = destination / "home"
    source_home = source / "home"
    if source_home.is_dir():
        shutil.copytree(source_home, home)
    else:
        home.mkdir(parents=True)
    sql_dir = source / "sql"
    if sql_dir.is_dir():
        root = home / ".local" / "share" / "opencode"
        root.mkdir(parents=True, exist_ok=True)
        for script in sorted(sql_dir.glob("*.sql")):
            database = root / script.name.removesuffix(".sql")
            connection = sqlite3.connect(database)
            try:
                connection.executescript(script.read_text(encoding="utf-8"))
                connection.commit()
            finally:
                connection.close()
    return home


def _context(home: Path, environ: dict[str, str] | None = None) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _by_label(outcome: DiscoveryOutcome) -> dict[str, SourceInstanceRecord]:
    return {record.opaque_label: record for record in outcome.records}


def _extract_by_label(
    adapter: OpenCodeAdapter, outcome: DiscoveryOutcome, tmp_path: Path
) -> dict[str, list[NormalizedUtterance]]:
    extracted: dict[str, list[NormalizedUtterance]] = {}
    for record in outcome.records:
        if record.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT":
            continue
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / "snaps" / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        extracted[record.opaque_label] = list(adapter.extract(record, target))
    return extracted


def test_fixture_meta_declarations() -> None:
    expected_kinds = {
        "success": "success",
        "empty": "empty",
        "malformed": "malformed",
        "unsupported": "unsupported",
        "migration": "migration",
    }
    for variant, kind in expected_kinds.items():
        meta = load_fixture_meta(FIXTURES / variant)
        assert meta.adapter_id == ADAPTER_ID
        assert meta.kind == kind
        assert meta.synthetic is True
        assert meta.storage_variant is not None


def test_factory_exposes_protocol_surface() -> None:
    adapter = create_adapter()
    assert adapter.adapter_id == ADAPTER_ID
    assert adapter.adapter_version == ADAPTER_VERSION
    assert adapter.stability is Stability.STABLE


def test_discover_success_two_instances(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 2
    by_label = _by_label(outcome)
    assert set(by_label) == {"OpenCode 1", "OpenCode 2"}

    alpha = by_label["OpenCode 1"]
    beta = by_label["OpenCode 2"]
    for record in (alpha, beta):
        assert record.accessibility is Accessibility.FOUND
        assert record.diagnostic_code is None
        assert record.storage_format == "sqlite+json"
        assert record.schema_fingerprint == "sqlite"
        assert record.app_version == "1.18.2"
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    # The overlapping channel database must not double-count msg_a01.
    assert alpha.candidate_messages == len(ALPHA_TEXTS)
    assert beta.candidate_messages == len(BETA_TEXTS)
    assert alpha.estimated_records == 9
    assert beta.estimated_records == 6
    assert alpha.candidate_words == sum(count_words(text) for text in ALPHA_TEXTS)
    assert beta.candidate_words == sum(count_words(text) for text in BETA_TEXTS)
    assert alpha.candidate_bytes == sum(len(text.encode("utf-8")) for text in ALPHA_TEXTS)
    assert alpha.earliest_timestamp == datetime(2026, 6, 1, 10, 1, tzinfo=UTC)
    assert alpha.latest_timestamp == datetime(2026, 6, 1, 11, 1, tzinfo=UTC)
    assert beta.earliest_timestamp == datetime(2026, 7, 1, 9, 1, tzinfo=UTC)
    assert beta.latest_timestamp == datetime(2026, 7, 1, 10, 2, tzinfo=UTC)


def test_discover_not_found_and_found_empty(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter()

    missing = adapter.discover(_context(tmp_path / "nobody-home"))
    assert len(missing.records) == 1
    assert missing.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing.records[0].diagnostic_code == "SOURCE_NOT_FOUND"

    root = tmp_path / "installed" / ".local" / "share" / "opencode"
    root.mkdir(parents=True)
    installed = adapter.discover(_context(tmp_path / "installed"))
    assert len(installed.records) == 1
    assert installed.records[0].accessibility is Accessibility.FOUND
    assert installed.records[0].diagnostic_code is None
    assert installed.records[0].schema_fingerprint == "empty"
    assert installed.records[0].candidate_messages == 0


def test_discover_empty_store_fixture(tmp_path: Path) -> None:
    home = _materialize("empty", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.opaque_label == "OpenCode 1"
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.schema_fingerprint == "sqlite"
    assert record.candidate_messages == 0
    assert record.candidate_words == 0
    assert record.estimated_records == 0
    assert record.earliest_timestamp is None


def test_xdg_data_home_and_opencode_db_overrides(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path / "store")
    data_home = home / ".local" / "share"

    adapter = OpenCodeAdapter()
    outcome = adapter.discover(
        _context(tmp_path / "other-home", environ={"XDG_DATA_HOME": str(data_home)})
    )
    assert len(outcome.records) == 2
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)

    without_override = adapter.discover(_context(tmp_path / "other-home"))
    assert without_override.records[0].accessibility is Accessibility.NOT_FOUND

    # Absolute OPENCODE_DB: the database lives outside any data root.
    moved = tmp_path / "elsewhere" / "history.db"
    moved.parent.mkdir(parents=True)
    shutil.copyfile(data_home / "opencode" / "opencode.db", moved)
    bare_home = tmp_path / "bare-home"
    (bare_home / ".local" / "share" / "opencode").mkdir(parents=True)
    absolute = OpenCodeAdapter().discover(_context(bare_home, environ={"OPENCODE_DB": str(moved)}))
    found = [r for r in absolute.records if r.accessibility is Accessibility.FOUND]
    assert sum(record.candidate_messages for record in found) == len(ALPHA_TEXTS) + len(BETA_TEXTS)

    # Filename OPENCODE_DB: joined onto the data root, outside the glob.
    renamed_home = tmp_path / "renamed-home"
    renamed_root = renamed_home / ".local" / "share" / "opencode"
    renamed_root.mkdir(parents=True)
    shutil.copyfile(data_home / "opencode" / "opencode.db", renamed_root / "renamed.db")
    named = OpenCodeAdapter().discover(
        _context(renamed_home, environ={"OPENCODE_DB": "renamed.db"})
    )
    found = [r for r in named.records if r.accessibility is Accessibility.FOUND]
    assert sum(record.candidate_messages for record in found) == len(ALPHA_TEXTS) + len(BETA_TEXTS)


def test_snapshot_sanitizes_databases_and_lists_every_file(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    record = _by_label(outcome)["OpenCode 1"]
    source = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source, target)

    on_disk = sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    )
    assert on_disk == sorted(entry.relative_path for entry in capture.files)
    names = {entry.relative_path for entry in capture.files}
    assert "opencode.db" in names
    assert "opencode-prod.db" in names
    assert "opencode.db.sanitized.json" in names
    assert "opencode-snapshot-meta.json" in names
    assert not any("session_share" in name for name in names)
    assert not any(name.endswith("auth.json") for name in names)

    for entry in capture.files:
        payload = (target / entry.relative_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry.sha256
        assert len(payload) == entry.size_bytes

    connection = sqlite3.connect(target / "opencode.db")
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == {"session", "message", "part"}
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT * FROM account").fetchall()
    finally:
        connection.close()
    # VACUUM after dropping the credential tables purges their pages: the
    # fake tokens, share secrets, and worktree paths are gone from the bytes.
    snapshot_bytes = (target / "opencode.db").read_bytes()
    assert b"FAKEFAKEFAKE" not in snapshot_bytes
    assert b"share-secret" not in snapshot_bytes
    assert b"/home/synthetic" not in snapshot_bytes


def test_extract_success_texts_flags_and_determinism(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    extracted = _extract_by_label(adapter, outcome, tmp_path)

    alpha = extracted["OpenCode 1"]
    beta = extracted["OpenCode 2"]
    assert {utterance.text for utterance in alpha} == ALPHA_TEXTS
    assert {utterance.text for utterance in beta} == BETA_TEXTS

    for utterance in [*alpha, *beta]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.authorship_confidence == pytest.approx(0.9)
        assert utterance.authorship_basis == "user_role_text_part_non_synthetic"
        assert "undated" not in utterance.content_flags
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")

    alpha_hash = _session_hash("ses_alpha01")
    planted = next(u for u in alpha if u.text.startswith("I very like"))
    assert planted.session_hash == alpha_hash
    assert planted.utterance_id.startswith(f"{ADAPTER_ID}-{alpha_hash[:16]}-msg_a01-")
    assert planted.timestamp == datetime(2026, 6, 1, 10, 1, tzinfo=UTC)

    # Synthetic, ignored, reasoning, file parts, the vendor template, the
    # child session, and assistant turns are all absent.
    all_texts = " ".join(u.text for u in [*alpha, *beta])
    assert "Read tool was called" not in all_texts
    assert "ignored by the user" not in all_texts
    assert "Summarize the repository structure" not in all_texts
    assert "outline of the login page" not in all_texts
    assert not any(u.text in VENDOR_TEMPLATE_TEXTS for u in [*alpha, *beta])

    archived = next(u for u in alpha if u.text.startswith("This task is finish"))
    assert archived.session_hash == _session_hash("ses_alpha02")

    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []

    # Re-extraction from the same snapshot is deterministic.
    record = _by_label(outcome)["OpenCode 1"]
    target = tmp_path / "snaps" / record.instance_key[:12]
    assert list(adapter.extract(record, target)) == alpha


def test_extract_refuses_unsanitized_snapshot(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    record = _by_label(outcome)["OpenCode 1"]
    target = tmp_path / "snap"
    adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    (target / "opencode.db.sanitized.json").unlink()
    with pytest.raises(PermissionError):
        list(adapter.extract(record, target))


def test_malformed_thresholds_and_undated_flag(tmp_path: Path) -> None:
    home = _materialize("malformed", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 3
    by_label = _by_label(outcome)

    ok = by_label["OpenCode 1"]
    db = by_label["OpenCode 2"]
    broken = by_label["OpenCode 3"]
    assert ok.accessibility is Accessibility.FOUND
    assert ok.candidate_messages == 5
    assert ok.estimated_records == 6
    assert ok.earliest_timestamp == datetime(2026, 3, 1, 10, 1, tzinfo=UTC)
    assert ok.latest_timestamp == datetime(2026, 3, 1, 10, 4, tzinfo=UTC)
    assert db.accessibility is Accessibility.FOUND
    assert db.candidate_messages == 1
    # The J1 store is over the 10% malformed threshold: unsupported, counted.
    assert broken.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert broken.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert broken.schema_fingerprint == "json-j1"
    assert broken.candidate_messages == 0

    extracted = _extract_by_label(adapter, outcome, tmp_path)
    ok_utterances = extracted["OpenCode 1"]
    assert len(ok_utterances) == 5
    assert not any("never be extracted" in u.text for u in ok_utterances)
    assert not any("store is unsupported" in u.text for u in ok_utterances)
    undated = [u for u in ok_utterances if "undated" in u.content_flags]
    assert len(undated) == 1
    assert undated[0].text.startswith("Remind me please")
    assert undated[0].timestamp is None
    db_utterances = extracted["OpenCode 2"]
    assert len(db_utterances) == 1
    assert db_utterances[0].text.startswith("The linter complain")
    assert extracted["OpenCode 3"] == []

    stats = adapter.extraction_stats(ok.instance_key)
    assert stats is not None
    assert stats.utterance_count == 5
    assert stats.unsupported_stores == ("json-j1",)
    assert stats.malformed_records == 4

    assert adapter.verify(ok, ok_utterances) == []
    assert adapter.verify(db, db_utterances) == []


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    home = _materialize("unsupported", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 3
    fingerprints = sorted(record.schema_fingerprint for record in outcome.records)
    assert fingerprints == ["json-j2", "sqlite", "sqlite"]
    for record in outcome.records:
        assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
        assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0

    extracted = _extract_by_label(adapter, outcome, tmp_path)
    assert all(utterances == [] for utterances in extracted.values())
    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []


def test_migration_dedup_across_generations(tmp_path: Path) -> None:
    home = _materialize("migration", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 3
    by_label = _by_label(outcome)

    old = by_label["OpenCode 1"]
    globals_ = by_label["OpenCode 2"]
    migrated = by_label["OpenCode 3"]
    for record in (old, globals_, migrated):
        assert record.accessibility is Accessibility.FOUND
        assert record.schema_fingerprint == "sqlite+json-j2+json-j1"

    assert old.candidate_messages == 3
    assert old.estimated_records == 3
    assert old.app_version == "0.3.5"
    assert old.earliest_timestamp == datetime(2025, 5, 1, 12, 1, tzinfo=UTC)
    assert globals_.candidate_messages == 1
    assert globals_.app_version == "0.3.5"
    assert globals_.earliest_timestamp == datetime(2025, 6, 1, 12, 1, tzinfo=UTC)
    assert migrated.candidate_messages == 2
    assert migrated.estimated_records == 2
    assert migrated.app_version == "1.2.0"
    assert migrated.earliest_timestamp == datetime(2026, 1, 1, 12, 1, tzinfo=UTC)
    assert migrated.latest_timestamp == datetime(2026, 1, 1, 12, 2, tzinfo=UTC)

    extracted = _extract_by_label(adapter, outcome, tmp_path)
    migrated_utterances = extracted["OpenCode 3"]
    assert {u.text for u in migrated_utterances} == {MIGRATION_M01, MIGRATION_M02}
    m_hash = _session_hash("ses_m01")
    assert all(u.session_hash == m_hash for u in migrated_utterances)
    kept = next(u for u in migrated_utterances if u.text == MIGRATION_M01)
    assert kept.content_flags == []
    # The SQLite copy of msg_m02 lost its parts; the J2 copy wins, flagged.
    fallback = next(u for u in migrated_utterances if u.text == MIGRATION_M02)
    assert "generation_text_mismatch" in fallback.content_flags

    old_texts = {u.text for u in extracted["OpenCode 1"]}
    assert old_texts == {
        "Why this build is failing since yesterday? I not changed anything.",
        "Please make the error message more informative for the user.",
        "I forgot how we call the deploy script, remind me please.",
    }
    assert {u.text for u in extracted["OpenCode 2"]} == {
        "Can you explain what mean this warning about the deprecated api?"
    }

    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []

    # The snapshot preserves the relative JSON layout of both legacy trees.
    record = by_label["OpenCode 3"]
    target = tmp_path / "snaps" / record.instance_key[:12]
    assert (target / "storage" / "migration").is_file()
    assert (target / "storage" / "part" / "msg_m01" / "prt_m01a.json").is_file()
    assert (
        target / "project" / "my-old-project" / "storage" / "session" / "info" / "ses_j101.json"
    ).is_file()


def test_wsl_host_store_is_hinted_never_read(tmp_path: Path) -> None:
    mount = tmp_path / "mnt"
    host_root = mount / "c" / "Users" / "fake-user" / ".local" / "share" / "opencode"
    host_root.mkdir(parents=True)
    (host_root / "opencode.db").write_bytes(b"never opened")

    adapter = OpenCodeAdapter(wsl_mount_base=mount)
    context = DiscoveryContext(
        os_environment=OsEnvironment.WSL,
        home=tmp_path / "wsl-home",
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ={},
    )
    outcome = adapter.discover(context)
    hints = [
        record
        for record in outcome.records
        if record.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT"
    ]
    assert len(hints) == 1
    assert hints[0].accessibility is Accessibility.INACCESSIBLE
    assert hints[0].schema_fingerprint == "wsl-host"
    assert hints[0].candidate_messages == 0
    assert outcome.instance_paths[hints[0].instance_key] == host_root
    with pytest.raises(PermissionError):
        adapter.snapshot(hints[0], host_root, tmp_path / "snap")

    # Outside WSL the mount base is never consulted.
    macos = OpenCodeAdapter(wsl_mount_base=mount).discover(_context(tmp_path / "wsl-home"))
    assert all(record.diagnostic_code != "SOURCE_WSL_HOST_STORE_HINT" for record in macos.records)


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    home = _materialize("success", tmp_path)
    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    record = _by_label(outcome)["OpenCode 2"]
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    assert "CARDINALITY_MISMATCH" in {d.code for d in adapter.verify(record, duplicated)}

    emptied = [extracted[0].model_copy(update={"text": "   "}), *extracted[1:]]
    assert "SCHEMA_INVALID_VALUE" in {d.code for d in adapter.verify(record, emptied)}

    template = [
        extracted[0].model_copy(update={"text": next(iter(VENDOR_TEMPLATE_TEXTS))}),
        *extracted[1:],
    ]
    assert "SCHEMA_INVALID_VALUE" in {d.code for d in adapter.verify(record, template)}

    stale = [
        extracted[0].model_copy(update={"timestamp": datetime(2015, 1, 1, tzinfo=UTC)}),
        *extracted[1:],
    ]
    assert "SCHEMA_INVALID_VALUE" in {d.code for d in adapter.verify(record, stale)}


def test_never_opens_denylisted_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _materialize("success", tmp_path)

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

    def recording_connect(target: Any, *args: Any, **kwargs: Any) -> Any:
        connected.append(str(target))
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    adapter = OpenCodeAdapter()
    outcome = adapter.discover(_context(home))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / "snaps" / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected recorded file reads"
    config_dir = home / ".config"
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not any(part in DENY_DIR_NAMES for part in path.parts), str(path)
        assert not path.is_relative_to(config_dir), str(path)

    assert connected, "expected recorded database connections"
    for target_name in connected:
        assert ".db" in target_name, target_name
        assert "auth" not in target_name, target_name
        assert "session_share" not in target_name, target_name

    for record in outcome.records:
        assert all(
            diagnostic.code != "SOURCE_SNAPSHOT_UNSAFE_PATH"
            for diagnostic in adapter.verify(record, [])
        )
