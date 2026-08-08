"""Fixture-driven tests for the Gemini CLI source adapter."""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.gemini_cli import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    GeminiCliAdapter,
    create_adapter,
)
from glite_english_audit.adapters.gemini_cli import adapter as adapter_module
from glite_english_audit.adapters.gemini_cli.adapter import DENY_FILE_NAMES
from glite_english_audit.adapters.gemini_cli.records import scan_json_session
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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini_cli"

SESSION_A = "aaaa1111-1111-4111-8111-111111111111"
SESSION_B_JSON = "bbbb2222-2222-4222-8222-222222222222"
SESSION_B_JSONL = "dddd4444-4444-4444-8444-444444444444"
SESSION_MIGRATION = "77770000-0000-4000-8000-000000000000"

INSTANCE_1_TEXTS = {
    "I very like this plan, can we start from the small part first?",
    "Please explain me this error message.\nI am not understand what it means.",
    "Look at @notes.txt and tell me what you think about my writing.",
    "Since many years I dream to speak English more fluent.",
    "This code works good, but I want make it more faster.",
    "<memory_hint>FAKE synthetic hint</memory_hint> Also please check the grammar in my "
    "commit message.",
    "Here is the screenshot, why the button looks so strange?",
    "And how I should name this variable better?",
}
INSTANCE_2_TEXTS = {
    "Yesterday I have wrote a long letter to my colleague about the deploy process.",
    "Please review @draft.md and correct all mistakes what you find.",
    "What is the more better way to say this phrase in formal email?",
    "Also I wanted to ask about the report what I attached.",
    "I am agree with your suggestion, let us continue with the second option.",
    "We should to check the config file, maybe the problem hides there.",
}
MALFORMED_TEXTS = {
    "The tests are failing since yesterday and I not sure why.",
    "Can you look on the second test? It seems flaky for me.",
    "Why the button is not clickable on the mobile version?",
    "I tried to reproduce the bug but it happens only sometimes.",
    "Maybe we should to add more logging here.",
    "The error disappears when I restart the server, it is very strange.",
    "How to explain this behavior to my manager in simple words?",
}


def _context(
    home: Path,
    environ: dict[str, str] | None = None,
    os_environment: OsEnvironment = OsEnvironment.MACOS,
) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=os_environment,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _extract_by_label(
    adapter: GeminiCliAdapter, outcome: DiscoveryOutcome, tmp_path: Path
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
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    assert len(outcome.records) == 2
    by_label = {record.opaque_label: record for record in outcome.records}
    assert set(by_label) == {"Gemini CLI 1", "Gemini CLI 2"}

    first = by_label["Gemini CLI 1"]
    second = by_label["Gemini CLI 2"]
    for record in (first, second):
        assert record.accessibility is Accessibility.FOUND
        assert record.stability is Stability.STABLE
        assert record.storage_format == "json/jsonl"
        assert record.app_version is None
        assert record.diagnostic_code is None
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    assert first.schema_fingerprint == "jsonl-v1+display+notices+kind+subagent-dirs+checkpoints"
    assert second.schema_fingerprint == "mixed"

    assert first.candidate_messages == len(INSTANCE_1_TEXTS)
    assert second.candidate_messages == len(INSTANCE_2_TEXTS)
    assert first.estimated_records == 19
    assert second.estimated_records == 8
    assert first.candidate_words == sum(count_words(text) for text in INSTANCE_1_TEXTS)
    assert second.candidate_words == sum(count_words(text) for text in INSTANCE_2_TEXTS)
    assert first.candidate_bytes == sum(len(text.encode("utf-8")) for text in INSTANCE_1_TEXTS)
    assert first.earliest_timestamp == datetime(2026, 6, 2, 9, 1, tzinfo=UTC)
    assert first.latest_timestamp == datetime(2026, 6, 2, 9, 30, tzinfo=UTC)
    assert second.earliest_timestamp == datetime(2026, 7, 5, 14, 1, tzinfo=UTC)
    assert second.latest_timestamp == datetime(2026, 7, 6, 10, 5, tzinfo=UTC)


def test_discover_not_found_and_found_empty(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()

    missing = adapter.discover(_context(tmp_path / "nobody-home"))
    assert len(missing.records) == 1
    assert missing.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing.records[0].diagnostic_code == "SOURCE_NOT_FOUND"

    (tmp_path / "installed" / ".gemini").mkdir(parents=True)
    installed = adapter.discover(_context(tmp_path / "installed"))
    assert len(installed.records) == 1
    assert installed.records[0].accessibility is Accessibility.FOUND
    assert installed.records[0].diagnostic_code is None
    assert installed.records[0].candidate_messages == 0
    assert installed.records[0].schema_fingerprint == "empty"

    (tmp_path / "installed" / ".gemini" / "tmp").mkdir()
    with_tmp = adapter.discover(_context(tmp_path / "installed"))
    assert len(with_tmp.records) == 1
    assert with_tmp.records[0].accessibility is Accessibility.FOUND
    assert with_tmp.records[0].candidate_messages == 0


def test_discover_empty_store_fixture() -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    assert len(outcome.records) == 2
    for record in outcome.records:
        assert record.accessibility is Accessibility.FOUND
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0
        assert record.earliest_timestamp is None
        assert record.schema_fingerprint == "empty"


def test_home_override_env(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    override = str(FIXTURES / "success" / "home")
    outcome = adapter.discover(_context(tmp_path, environ={"GEMINI_CLI_HOME": override}))
    assert len(outcome.records) == 2
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)

    dangling = str(tmp_path / "not-a-gemini-home")
    fallback = adapter.discover(_context(tmp_path, environ={"GEMINI_CLI_HOME": dangling}))
    assert fallback.records[0].accessibility is Accessibility.NOT_FOUND


def test_wsl_host_store_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "_WSL_MOUNT_ROOT", tmp_path / "mnt")
    (tmp_path / "mnt" / "c" / "Users" / "fake-user" / ".gemini" / "tmp").mkdir(parents=True)

    home = tmp_path / "linux-home"
    chats = home / ".gemini" / "tmp" / ("1" * 64) / "chats"
    chats.mkdir(parents=True)
    (chats / "session-2026-02-02T08-00-12121212.jsonl").write_text(
        '{"sessionId": "12121212-1212-4121-8121-121212121212", '
        '"projectHash": "' + "1" * 64 + '", "startTime": "2026-02-02T08:00:00Z"}\n'
        '{"id": "w1", "timestamp": "2026-02-02T08:01:00Z", "type": "user", '
        '"content": "The linux side store works good for me."}\n',
        encoding="utf-8",
    )

    adapter = GeminiCliAdapter()
    context = _context(
        home,
        environ={"USERPROFILE": "C:\\Users\\fake-user"},
        os_environment=OsEnvironment.WSL,
    )
    outcome = adapter.discover(context)
    assert len(outcome.records) == 2
    by_label = {record.opaque_label: record for record in outcome.records}
    wsl_side = by_label["Gemini CLI 1"]
    host_side = by_label["Gemini CLI 2"]

    assert wsl_side.stability is Stability.STABLE
    assert wsl_side.candidate_messages == 1
    assert host_side.stability is Stability.EXPERIMENTAL
    assert host_side.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT"
    assert host_side.schema_fingerprint == "wsl-host-untested"
    assert host_side.candidate_messages == 0
    assert wsl_side.instance_key != host_side.instance_key

    non_wsl = adapter.discover(_context(home, environ={"USERPROFILE": "C:\\Users\\fake-user"}))
    assert len(non_wsl.records) == 1


def test_snapshot_manifest_completeness(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Gemini CLI 1")
    source_dir = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source_dir, target)

    expected = {
        "chats/session-2026-06-02T09-00-aaaa1111.jsonl",
        "chats/session-2026-06-02T11-00-cccc3333.jsonl",
        "gemini-cli-source-paths.json",
    }
    assert {entry.relative_path for entry in capture.files} == expected

    session_entry = next(
        entry
        for entry in capture.files
        if entry.relative_path.endswith("session-2026-06-02T09-00-aaaa1111.jsonl")
    )
    source_bytes = (source_dir / "chats" / "session-2026-06-02T09-00-aaaa1111.jsonl").read_bytes()
    assert session_entry.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert session_entry.size_bytes == len(source_bytes)
    assert (target / session_entry.relative_path).read_bytes() == source_bytes

    # The capture doubles as the cleanup manifest: it must list every file
    # under the snapshot directory, and nothing else.
    on_disk = {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()}
    assert on_disk == expected
    assert not any("aaaa1111-1111-4111-8111-111111111111" in name for name in on_disk)
    assert not any(name.startswith("logs") for name in on_disk)

    if os.name == "posix":
        assert (target / "chats").stat().st_mode & 0o777 == 0o700
        assert (target / session_entry.relative_path).stat().st_mode & 0o777 == 0o600


def test_extract_success_texts_flags_and_determinism(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    extracted = _extract_by_label(adapter, outcome, tmp_path)

    first = extracted["Gemini CLI 1"]
    second = extracted["Gemini CLI 2"]
    assert {u.text for u in first} == INSTANCE_1_TEXTS
    assert {u.text for u in second} == INSTANCE_2_TEXTS
    assert not any("must never be extracted" in u.text for u in [*first, *second])

    for utterance in [*first, *second]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")

    hash_a = _session_hash(SESSION_A)
    display = next(u for u in first if u.text.startswith("Look at @notes.txt"))
    assert display.utterance_id == f"{ADAPTER_ID}-{hash_a[:16]}-m03"
    assert display.session_hash == hash_a
    assert display.authorship_basis == "explicit_user_role+display_content_preferred"
    assert display.authorship_confidence == pytest.approx(0.95)
    assert display.content_flags == ["expanded_content_present"]

    plain = next(u for u in first if u.text.startswith("I very like"))
    assert plain.authorship_basis == "explicit_user_role"
    assert plain.authorship_confidence == pytest.approx(0.9)
    assert plain.content_flags == []

    multipart = next(u for u in first if u.text.startswith("Please explain me"))
    assert multipart.content_flags == ["multipart_no_display"]

    # A rewritten message ID keeps only its last occurrence (spec 6.3).
    rewritten = next(u for u in first if u.text.startswith("Since many years"))
    assert rewritten.text.endswith("more fluent.")
    assert rewritten.timestamp == datetime(2026, 6, 2, 9, 20, tzinfo=UTC)

    unknown_wrapper = next(u for u in first if u.text.startswith("<memory_hint>"))
    assert unknown_wrapper.content_flags == ["unknown_wrapper"]

    trimmed = next(u for u in second if u.text.startswith("Please review @draft.md"))
    assert trimmed.content_flags == ["reference_expansion_trimmed"]
    embedded = next(u for u in second if u.text.startswith("Also I wanted"))
    assert embedded.content_flags == ["reference_expansion_trimmed"]
    assert "referenced files" not in embedded.text

    json_hash = _session_hash(SESSION_B_JSON)
    jsonl_hash = _session_hash(SESSION_B_JSONL)
    assert {u.session_hash for u in second} == {json_hash, jsonl_hash}

    record = next(r for r in outcome.records if r.opaque_label == "Gemini CLI 1")
    target = tmp_path / "snapshots" / record.instance_key[:12]
    again = list(adapter.extract(record, target))
    assert again == first

    assert adapter.verify(record, first) == []


def test_malformed_file_thresholds(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "jsonl-v1"
    assert record.candidate_messages == 7
    assert record.estimated_records == 13

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert {u.text for u in extracted} == MALFORMED_TEXTS
    assert not any("FAKE" in u.text for u in extracted)
    assert not any("This line was cut" in u.text for u in extracted)

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.utterance_count == 7
    assert stats.truncated_tail_files == ("session-2026-05-01T10-00-1111aaaa.jsonl",)
    assert stats.unsupported_files == ("session-2026-05-03T08-00-3333cccc.jsonl",)
    assert stats.malformed_files == ("session-2026-05-04T07-00-4444dddd.json",)
    assert stats.malformed_lines == 4

    assert adapter.verify(record, extracted) == []


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "unsupported" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "unsupported"
    assert record.candidate_messages == 0
    assert record.candidate_words == 0
    assert record.estimated_records == 0

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert extracted == []
    assert adapter.verify(record, extracted) == []


def test_migration_pair_dedup(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "mixed"
    assert record.candidate_messages == 3
    assert record.estimated_records == 4

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert len(extracted) == 3
    suffixes = {u.utterance_id.rsplit("-", 1)[-1] for u in extracted}
    assert suffixes == {"mg01", "mg03", "mg04"}
    assert len({u.utterance_id for u in extracted}) == 3
    migration_hash = _session_hash(SESSION_MIGRATION)
    assert all(u.session_hash == migration_hash for u in extracted)
    texts = [u.text for u in extracted]
    assert len(texts) == len(set(texts))

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.dropped_duplicate_files == ("session-2026-03-10T09-00-77770000.json",)

    assert adapter.verify(record, extracted) == []


def test_session_meta_missing_fallback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    chats = home / ".gemini" / "tmp" / ("b" * 64) / "chats"
    chats.mkdir(parents=True)
    stem = "session-2026-02-01T12-00-9999aaaa"
    (chats / f"{stem}.jsonl").write_text(
        '{"id": "z01", "timestamp": "2026-02-01T12:01:00Z", "type": "user", '
        '"content": "The deploy failed again, I attached the log below."}\n'
        '{"id": "z02", "timestamp": "2026-02-01T12:03:00Z", "type": "user", '
        '"content": "Sorry, I forgot to say hello in my previous message."}\n',
        encoding="utf-8",
    )
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 2

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert len(extracted) == 2
    assert all("session_meta_missing" in u.content_flags for u in extracted)
    assert all(u.session_hash == _session_hash(stem) for u in extracted)
    assert adapter.verify(record, extracted) == []


def test_oversize_json_file_is_never_read(tmp_path: Path) -> None:
    path = tmp_path / "session-2026-01-01T00-00-abcd1234.json"
    path.write_text(json.dumps({"sessionId": "abcd", "messages": []}), encoding="utf-8")
    scan = scan_json_session(path, size_cap_bytes=4)
    assert scan.oversize_skipped is True
    assert scan.extractable is False
    assert scan.kept == []


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Gemini CLI 2")
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    codes = {d.code for d in adapter.verify(record, duplicated)}
    assert "CARDINALITY_MISMATCH" in codes

    slash = [
        extracted[0].model_copy(update={"text": "/compress everything now"}),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, slash)}
    assert "SCHEMA_INVALID_VALUE" in codes

    wrapped = [
        extracted[0].model_copy(update={"text": "<session_context>leftover</session_context>"}),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, wrapped)}
    assert "SCHEMA_INVALID_VALUE" in codes

    stale = [
        extracted[0].model_copy(update={"timestamp": datetime(2015, 1, 1, tzinfo=UTC)}),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, stale)}
    assert "SCHEMA_INVALID_VALUE" in codes


def test_never_opens_denylisted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []
    connected: list[object] = []
    original_open = Path.open
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

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
        connected.append(args)
        msg = "the gemini_cli adapter must never touch SQLite"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    adapter = GeminiCliAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected session-file reads to be recorded"
    forbidden_dirs = {"history", "checkpoints", "aaaa1111-1111-4111-8111-111111111111"}
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not path.name.startswith("checkpoint-"), str(path)
        assert path.name != ".project_root", str(path)
        assert not any(part in forbidden_dirs for part in path.parts), str(path)
    assert connected == []

    opened_names = {path.name for path in opened}
    assert "session-2026-06-02T09-00-aaaa1111.jsonl" in opened_names
    assert "session-2026-07-05T14-00-bbbb2222.json" in opened_names
    assert "session-2026-07-06T10-00-dddd4444.jsonl" in opened_names
    assert "agent-01.jsonl" not in opened_names

    with pytest.raises(PermissionError):
        adapter._scan_file(FIXTURES / "success" / "home" / ".gemini" / "oauth_creds.json")
