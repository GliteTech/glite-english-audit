"""Fixture-driven tests for the Aider source adapter."""

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.aider import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    AiderAdapter,
    create_adapter,
)
from glite_english_audit.adapters.aider.adapter import (
    CHAT_MARKDOWN_NAME,
    DENY_FILE_NAMES,
    INPUT_HISTORY_ENV,
    INPUT_HISTORY_NAME,
)
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.discovery.base import DiscoveryContext, DiscoveryOutcome
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.verification.fixture_policy import load_fixture_meta

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "aider"

WEBAPP_TEXTS = [
    "I did not received any error yet, the page is just empty.",
    "Please explain me how the auth flow works, I am not sure I understand it.",
    "I very like this plan, let's do it this way.\nPlease also update the readme file after.",
    "What we should check first when the deploy fails?\n\n"
    "Maybe the config file is wrong since many years?",
]
NOTES_TEXTS = [
    "Can you help me to improve this paragraph? It sounds not natural for me.",
    "Today I written a long note about my learning progress.\nPlease check the grammar of it.",
    "I am agree that the second variant reads better.",
    "Since many years I want to write more clear emails, can you correct my draft?",
    "Results summary",
]


def _context(home: Path, environ: dict[str, str] | None = None) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _extract_by_label(
    adapter: AiderAdapter, outcome: DiscoveryOutcome, tmp_path: Path
) -> dict[str, list[NormalizedUtterance]]:
    extracted: dict[str, list[NormalizedUtterance]] = {}
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / "snapshots" / record.instance_key[:12]
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


def test_discover_success_two_instances() -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    assert len(outcome.records) == 2
    by_label = {record.opaque_label: record for record in outcome.records}
    assert set(by_label) == {"Aider 1", "Aider 2"}

    webapp = by_label["Aider 1"]
    notes = by_label["Aider 2"]
    for record in (webapp, notes):
        assert record.accessibility is Accessibility.FOUND
        assert record.diagnostic_code is None
        assert record.storage_format == "text"
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    assert webapp.schema_fingerprint == "input-history-v1;lf"
    assert notes.schema_fingerprint == "chat-markdown-v1;crlf"

    assert webapp.candidate_messages == len(WEBAPP_TEXTS)
    assert webapp.estimated_records == 9
    assert webapp.candidate_words == sum(count_words(text) for text in WEBAPP_TEXTS)
    assert webapp.candidate_bytes == sum(len(text.encode("utf-8")) for text in WEBAPP_TEXTS)
    assert webapp.earliest_timestamp == datetime(2025, 6, 2, 9, 15, 4, 123456)
    assert webapp.latest_timestamp == datetime(2025, 6, 2, 9, 40, 0, 111111)

    assert notes.candidate_messages == len(NOTES_TEXTS)
    assert notes.estimated_records == 7
    assert notes.candidate_words == sum(count_words(text) for text in NOTES_TEXTS)
    assert notes.earliest_timestamp == datetime(2025, 7, 10, 14, 2, 11)
    assert notes.latest_timestamp == datetime(2025, 7, 11, 9, 30, 0)

    webapp_dir = (FIXTURES / "success" / "home" / "projects" / "webapp").resolve()
    assert webapp.path_hash == hashlib.sha256(str(webapp_dir).encode("utf-8")).hexdigest()


def test_discover_not_found(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    home = tmp_path / "nobody-home"
    home.mkdir()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.NOT_FOUND
    assert record.diagnostic_code == "SOURCE_NOT_FOUND"
    assert record.schema_fingerprint == "absent"
    assert record.candidate_messages == 0


def test_discover_empty_fixture() -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    assert len(outcome.records) == 2
    fingerprints = {record.schema_fingerprint for record in outcome.records}
    assert fingerprints == {"input-history-v1;none", "chat-markdown-v1;lf"}
    for record in outcome.records:
        assert record.accessibility is Accessibility.FOUND
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0
        assert record.earliest_timestamp is None


def test_env_override_discovers_relocated_instance(tmp_path: Path) -> None:
    env_file = FIXTURES / "success" / "home" / "relocated" / "custom-input.history"
    adapter = AiderAdapter()
    outcome = adapter.discover(
        _context(FIXTURES / "success" / "home", environ={INPUT_HISTORY_ENV: str(env_file)})
    )
    assert len(outcome.records) == 3
    by_label = {record.opaque_label: record for record in outcome.records}
    relocated = by_label["Aider 3"]
    assert relocated.accessibility is Accessibility.FOUND
    assert relocated.schema_fingerprint == "input-history-v1;lf"
    assert relocated.candidate_messages == 2
    assert relocated.earliest_timestamp == datetime(2026, 1, 5, 8, 0, 0, 250000)

    extracted = _extract_by_label(adapter, outcome, tmp_path)["Aider 3"]
    assert len(extracted) == 2
    session_hash = _path_hash(env_file)
    for utterance in extracted:
        assert utterance.session_hash == session_hash
        assert utterance.source_path_hash == session_hash
    assert extracted[0].text.startswith("This relocated history file")
    assert adapter.verify(relocated, extracted) == []


def test_env_override_missing_target_ignored(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(
        _context(
            FIXTURES / "empty" / "home",
            environ={INPUT_HISTORY_ENV: str(tmp_path / "missing.history")},
        )
    )
    assert len(outcome.records) == 2


def test_scan_prunes_dot_git_and_collapses_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    (home / ".git" / INPUT_HISTORY_NAME).write_text(
        "\n# 2025-01-01 00:00:00\n+decoy inside a pruned directory\n", encoding="utf-8"
    )
    proj = home / "proj"
    proj.mkdir()
    real = proj / INPUT_HISTORY_NAME
    real.write_text(
        "\n# 2025-02-02 10:00:00\n+Hello from the original project.\n", encoding="utf-8"
    )
    alias = home / "alias"
    alias.mkdir()
    (alias / INPUT_HISTORY_NAME).symlink_to(real)

    adapter = AiderAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 1
    assert outcome.records[0].candidate_messages == 1


def test_snapshot_manifest_is_the_cleanup_plan(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Aider 1")
    source_dir = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source_dir, target)

    listed = sorted(entry.relative_path for entry in capture.files)
    on_disk = sorted(path.name for path in target.rglob("*") if path.is_file())
    assert listed == on_disk

    entry = next(e for e in capture.files if e.relative_path == INPUT_HISTORY_NAME)
    source_bytes = (source_dir / INPUT_HISTORY_NAME).read_bytes()
    assert entry.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert entry.size_bytes == len(source_bytes)
    assert (target / INPUT_HISTORY_NAME).read_bytes() == source_bytes

    # Channel priority: the chat transcript is never copied for this instance.
    assert CHAT_MARKDOWN_NAME not in on_disk
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o700
        assert (target / INPUT_HISTORY_NAME).stat().st_mode & 0o777 == 0o600


def test_extract_success_golden(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    extracted = _extract_by_label(adapter, outcome, tmp_path)
    webapp = extracted["Aider 1"]
    notes = extracted["Aider 2"]

    assert [u.text for u in webapp] == WEBAPP_TEXTS
    assert [u.text for u in notes] == NOTES_TEXTS

    for utterance in [*webapp, *notes]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert not utterance.text.strip().startswith(("/", "!"))
        assert "decoy" not in utterance.text
        assert "fence" not in utterance.text
        assert utterance.text != "<blank>"

    webapp_file = FIXTURES / "success" / "home" / "projects" / "webapp" / INPUT_HISTORY_NAME
    webapp_hash = _path_hash(webapp_file)
    assert [u.utterance_id for u in webapp] == [
        f"{ADAPTER_ID}-{webapp_hash[:16]}-e{ordinal}" for ordinal in (0, 1, 3, 7)
    ]
    for utterance in webapp:
        assert utterance.session_hash == webapp_hash
        assert utterance.source_path_hash == webapp_hash
        assert utterance.authorship_basis == "input_history_prompt_entry"
        assert utterance.authorship_confidence == pytest.approx(0.95)
    assert webapp[0].timestamp is None
    assert webapp[0].content_flags == ["undated"]
    assert webapp[1].timestamp == datetime(2025, 6, 2, 9, 15, 4, 123456)
    assert webapp[1].content_flags == ["timezone_unknown"]

    notes_file = FIXTURES / "success" / "home" / "projects" / "notes" / CHAT_MARKDOWN_NAME
    notes_hash = _path_hash(notes_file)
    session_1 = hashlib.sha256(f"{notes_hash}:1".encode()).hexdigest()
    session_2 = hashlib.sha256(f"{notes_hash}:2".encode()).hexdigest()
    assert [u.utterance_id for u in notes] == [
        f"{ADAPTER_ID}-{session_1[:16]}-b1m0",
        f"{ADAPTER_ID}-{session_1[:16]}-b1m1",
        f"{ADAPTER_ID}-{session_2[:16]}-b2m0",
        f"{ADAPTER_ID}-{session_2[:16]}-b2m1",
        f"{ADAPTER_ID}-{session_2[:16]}-b2m2",
    ]
    for utterance in notes:
        assert utterance.source_path_hash == notes_hash
        assert utterance.authorship_basis == "chat_markdown_user_prefix"
        assert utterance.authorship_confidence == pytest.approx(0.7)
        assert "fallback_channel" in utterance.content_flags
        assert "session_start_time_only" in utterance.content_flags
    assert notes[0].timestamp == datetime(2025, 7, 10, 14, 2, 11)
    assert notes[2].timestamp == datetime(2025, 7, 11, 9, 30, 0)
    assert notes[0].session_hash != notes[2].session_hash

    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []

    # Re-extraction from the same snapshot is deterministic.
    record = next(r for r in outcome.records if r.opaque_label == "Aider 1")
    target = tmp_path / "snapshots" / record.instance_key[:12]
    assert list(adapter.extract(record, target)) == webapp


def test_malformed_variants(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    assert len(outcome.records) == 3
    found = [r for r in outcome.records if r.accessibility is Accessibility.FOUND]
    unsupported = [
        r for r in outcome.records if r.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    ]
    assert len(found) == 1
    assert len(unsupported) == 2
    for record in unsupported:
        assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
        assert record.schema_fingerprint == "unsupported"
        assert record.candidate_messages == 0

    truncated = found[0]
    assert truncated.schema_fingerprint == "input-history-v1;lf"
    assert truncated.candidate_messages == 2
    assert truncated.estimated_records == 3

    extracted = _extract_by_label(adapter, outcome, tmp_path)
    kept = extracted[truncated.opaque_label]
    assert len(kept) == 2
    assert all("interr" not in u.text for u in kept)
    assert "truncated_tail_dropped" in kept[-1].content_flags
    stats = adapter.extraction_stats(truncated.instance_key)
    assert stats is not None
    assert stats.truncated_tail is True

    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []
    for record in unsupported:
        assert extracted[record.opaque_label] == []


def test_unsupported_fails_closed_and_falls_back(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "unsupported" / "home"))
    assert len(outcome.records) == 2
    by_fingerprint = {record.schema_fingerprint: record for record in outcome.records}
    assert set(by_fingerprint) == {"unsupported", "chat-markdown-v1-fallback;lf"}

    prose = by_fingerprint["unsupported"]
    assert prose.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert prose.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert prose.candidate_messages == 0

    fallback = by_fingerprint["chat-markdown-v1-fallback;lf"]
    assert fallback.accessibility is Accessibility.FOUND
    assert fallback.candidate_messages == 2

    extracted = _extract_by_label(adapter, outcome, tmp_path)
    assert extracted[prose.opaque_label] == []
    kept = extracted[fallback.opaque_label]
    assert [u.text for u in kept] == [
        "My colleague said me that this config is wrong, can you check it?",
        "How it is possible that the tests pass locally but fail on the CI?",
    ]
    for utterance in kept:
        assert "fallback_channel" in utterance.content_flags
        assert utterance.authorship_basis == "chat_markdown_user_prefix"
    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []


def test_migration_legacy_generation(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "input-history-v1;lf"
    assert record.candidate_messages == 2
    assert record.earliest_timestamp == datetime(2023, 8, 14, 19, 21, 5)
    assert record.latest_timestamp == datetime(2023, 8, 14, 19, 25, 44)

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert [u.text for u in extracted] == [
        "Please explain me what this repo map feature does, I could not find the docs.",
        "I am not agree with this approach, make it more simple please.",
    ]
    # Channel priority: the legacy chat transcript is never opened.
    assert all(path.name != CHAT_MARKDOWN_NAME for path in adapter._opened_paths)
    assert adapter.verify(record, extracted) == []


def test_encoding_below_threshold_keeps_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    padding = "This entry has plenty of clean English words to keep the ratio low. " * 5
    data = (
        f"\n# 2025-02-01 10:00:00\n+{padding}\n\n# 2025-02-01 10:05:00\n+caf".encode()
        + b"\xff latte was great\n"
    )
    (proj / INPUT_HISTORY_NAME).write_bytes(data)

    adapter = AiderAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 2
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert "�" in extracted[1].text


def test_encoding_above_threshold_quarantines(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / INPUT_HISTORY_NAME).write_bytes(b"# 2025-02-01 10:00:00\n+" + b"\xff" * 200 + b"\n")

    adapter = AiderAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Aider 1")
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    codes = {d.code for d in adapter.verify(record, duplicated)}
    assert "CARDINALITY_MISMATCH" in codes

    command_text = [
        extracted[0].model_copy(update={"text": "/run rm -rf build"}),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, command_text)}
    assert "SCHEMA_INVALID_VALUE" in codes

    stale = [
        extracted[1].model_copy(update={"timestamp": datetime(2031, 1, 1, 0, 0)}),
        *extracted[:1],
        *extracted[2:],
    ]
    codes = {d.code for d in adapter.verify(record, stale)}
    assert "SCHEMA_INVALID_VALUE" in codes


def test_never_opens_denylisted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []
    original_open = Path.open
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    connect_calls: list[object] = []

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return original_open(self, *args, **kwargs)

    def recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self)
        return original_read_text(self, *args, **kwargs)

    def recording_read_bytes(self: Path) -> bytes:
        opened.append(self)
        return original_read_bytes(self)

    def recording_connect(*args: Any, **kwargs: Any) -> Any:
        connect_calls.append(args)
        msg = "the aider adapter must never open a database"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    home = FIXTURES / "success" / "home"
    env_file = home / "relocated" / "custom-input.history"
    adapter = AiderAdapter()
    outcome = adapter.discover(_context(home, environ={INPUT_HISTORY_ENV: str(env_file)}))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected history reads to be recorded"
    webapp_chat = (home / "projects" / "webapp" / CHAT_MARKDOWN_NAME).resolve()
    resolved_opened = {str(path.resolve()) for path in opened}
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert ".aider" not in path.parts, str(path)
        assert not any(part.startswith(".aider.tags.cache") for part in path.parts), str(path)
    # Channel priority: the webapp chat transcript stays untouched.
    assert str(webapp_chat) not in resolved_opened
    # Pruned decoys are never opened.
    assert not any("node_modules" in path.parts or "deep" in path.parts for path in opened)
    assert connect_calls == []
    audit_codes = {
        diagnostic.code
        for record in outcome.records
        for diagnostic in adapter.verify(record, [])
        if diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    }
    assert audit_codes == set()
