"""Fixture-driven tests for the Roo Code source adapter."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.roo_code import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    RooCodeAdapter,
    create_adapter,
)
from glite_english_audit.adapters.roo_code import adapter as roo_adapter_module
from glite_english_audit.adapters.roo_code.adapter import DENY_DIR_NAMES, DENY_FILE_NAMES
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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "roo_code"

ROO_EXT = "rooveterinaryinc.roo-cline"

TASK_A1 = "019537a0-0000-7000-8000-00000000a001"
TASK_A2 = "019537a1-0000-7000-8000-00000000a002"
TASK_B1 = "6f6e0001-2222-4333-8444-00000000b001"

INSTANCE_1_TEXTS = {
    "Please explain me how this parser works, I am not sure I understand it.",
    "I very like this plan, please continue with it.",
    "How I can make the error message more clear for users?",
    "I am agree with the first part, but do not touch the config file.",
    "Please also look at 'src/config.ts' (see below for file content) and tell me what is wrong.",
    "Since many years I want to improve my English writing, please check also the readme wording.",
    "Can you help me to write more better commit messages?",
    "Please explain shorter, I did not received so much time today.",
    "Please check the <placeholder> tag handling too.",
}
INSTANCE_2_TEXTS = {
    "Today I written a small script for backup, please review it.",
    "It sounds not natural for me, let's rename the function.",
    "Yes, I prefer the second variant because it is more simple.",
}
INSTANCE_3_TEXTS = {
    "My English is not so good, please correct me when I write something wrong.",
    "How looks the final report? I want to see it before we send.",
}
MIGRATION_TEXTS = {
    "Please help me to translate this old build script, it use very strange syntax.",
    "Do not open this file, it have nothing interesting inside.",
    "For the first question my answer is yes, we can try it tomorrow.",
    "Let's continue, I founded one more place where the script fails.",
    "Check please the login form, users report it behaves strange on mobile.",
    "Wait, we not need this change in the same file.",
    "Now I understand the problem more better, thank you for the patience.",
    "Good, and please write the summary with simple words.",
    "Start the FAKE cleanup task, I will give more details later.",
    "I returned back to this task, let's finish it today.",
}


def _context(home: Path, environ: dict[str, str] | None = None) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _ts(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


def _session_hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _extract_by_label(
    adapter: RooCodeAdapter, outcome: DiscoveryOutcome, tmp_path: Path
) -> dict[str, list[NormalizedUtterance]]:
    extracted: dict[str, list[NormalizedUtterance]] = {}
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / "snapshots" / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        extracted[record.opaque_label] = list(adapter.extract(record, target))
    return extracted


def _write_min_task(root: Path, task_id: str, ts_ms: int, text: str) -> None:
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    payload = [
        {
            "role": "user",
            "ts": ts_ms,
            "content": [{"type": "text", "text": f"<user_message>\n{text}\n</user_message>"}],
        },
        {
            "role": "assistant",
            "ts": ts_ms + 1000,
            "content": [{"type": "text", "text": "FAKE synthetic reply."}],
        },
    ]
    (task_dir / "api_conversation_history.json").write_text(json.dumps(payload), encoding="utf-8")


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


def test_discover_success_three_instances() -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    assert len(outcome.records) == 3
    by_label = {record.opaque_label: record for record in outcome.records}
    assert set(by_label) == {"Roo Code 1", "Roo Code 2", "Roo Code 3"}

    first = by_label["Roo Code 1"]
    second = by_label["Roo Code 2"]
    third = by_label["Roo Code 3"]
    for record in (first, second, third):
        assert record.accessibility is Accessibility.FOUND
        assert record.diagnostic_code is None
        assert record.storage_format == "json"
        assert record.app_version is None
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    # Labels follow earliest activity: stable March, nightly April, VSCodium May.
    assert first.stability is Stability.STABLE
    assert second.stability is Stability.STABLE
    assert third.stability is Stability.BETA
    assert ROO_EXT in outcome.instance_paths[first.instance_key].parts
    assert "rooveterinaryinc.roo-code-nightly" in outcome.instance_paths[second.instance_key].parts
    assert "VSCodium" in outcome.instance_paths[third.instance_key].parts

    assert first.schema_fingerprint == "g3-user-message+meta+ts"
    assert second.schema_fingerprint == "g2-native-json+ts"
    assert third.schema_fingerprint == "g3-user-message+meta+ts"

    assert first.candidate_messages == len(INSTANCE_1_TEXTS)
    assert second.candidate_messages == len(INSTANCE_2_TEXTS)
    assert third.candidate_messages == len(INSTANCE_3_TEXTS)
    assert first.estimated_records == 13
    assert second.estimated_records == 6
    assert third.estimated_records == 3
    assert first.candidate_words == sum(count_words(text) for text in INSTANCE_1_TEXTS)
    assert second.candidate_words == sum(count_words(text) for text in INSTANCE_2_TEXTS)
    assert first.candidate_bytes == sum(len(text.encode("utf-8")) for text in INSTANCE_1_TEXTS)
    assert first.earliest_timestamp == _ts(1772359200000)
    assert first.latest_timestamp == _ts(1772442180000)


def test_discover_not_found_and_found_empty(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()

    missing = adapter.discover(_context(tmp_path / "nobody-home"))
    assert len(missing.records) == 1
    assert missing.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing.records[0].diagnostic_code == "SOURCE_NOT_FOUND"
    assert missing.records[0].schema_fingerprint == "absent"

    home = tmp_path / "installed"
    root = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / ROO_EXT
    root.mkdir(parents=True)
    installed = adapter.discover(_context(home))
    assert len(installed.records) == 1
    assert installed.records[0].accessibility is Accessibility.FOUND
    assert installed.records[0].diagnostic_code is None
    assert installed.records[0].candidate_messages == 0
    assert installed.records[0].schema_fingerprint == "empty"

    (root / "tasks").mkdir()
    with_tasks = adapter.discover(_context(home))
    assert with_tasks.records[0].accessibility is Accessibility.FOUND
    assert with_tasks.records[0].candidate_messages == 0


def test_discover_empty_store_fixture() -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    assert len(outcome.records) == 2
    for record in outcome.records:
        assert record.accessibility is Accessibility.FOUND
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0
        assert record.earliest_timestamp is None
        assert record.schema_fingerprint == "empty"


def test_snapshot_copies_allowlisted_task_files(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Roo Code 1")
    source_dir = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source_dir, target)
    assert capture.snapshot_relative_dir == target.name

    a1_sub = _session_hash(TASK_A1)[:16]
    a2_sub = _session_hash(TASK_A2)[:16]
    expected = {
        f"{a1_sub}/api_conversation_history.json",
        f"{a1_sub}/history_item.json",
        f"{a1_sub}/ui_messages.json",
        f"{a2_sub}/api_conversation_history.json",
        f"{a2_sub}/history_item.json",
        f"{a2_sub}/ui_messages.json",
        "roo-code-session-hashes.json",
    }
    assert {entry.relative_path for entry in capture.files} == expected

    source_bytes = (source_dir / "tasks" / TASK_A1 / "api_conversation_history.json").read_bytes()
    api_entry = next(
        entry
        for entry in capture.files
        if entry.relative_path == f"{a1_sub}/api_conversation_history.json"
    )
    assert api_entry.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert api_entry.size_bytes == len(source_bytes)
    assert (target / a1_sub / "api_conversation_history.json").read_bytes() == source_bytes

    on_disk = sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    )
    assert on_disk == sorted(entry.relative_path for entry in capture.files)
    copied_names = {path.name for path in target.rglob("*") if path.is_file()}
    assert "task_metadata.json" not in copied_names
    assert "claude_messages.json" not in copied_names
    assert "_index.json" not in copied_names
    assert not any(name.endswith(".lock") for name in copied_names)
    assert not any("checkpoints" in path.parts for path in target.rglob("*"))


def test_extract_success_texts_flags_and_determinism(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    extracted = _extract_by_label(adapter, outcome, tmp_path)

    first = extracted["Roo Code 1"]
    second = extracted["Roo Code 2"]
    third = extracted["Roo Code 3"]
    assert {u.text for u in first} == INSTANCE_1_TEXTS
    assert {u.text for u in second} == INSTANCE_2_TEXTS
    assert {u.text for u in third} == INSTANCE_3_TEXTS

    for utterance in [*first, *second, *third]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")
        assert "DECOY" not in utterance.text
        assert "environment_details" not in utterance.text
        assert "file_content" not in utterance.text

    all_texts = {u.text for u in [*first, *second, *third]}
    assert not any("condensation summary" in text for text in all_texts)
    assert not any("FAKE synthetic file body" in text for text in all_texts)

    a1_hash = _session_hash(TASK_A1)
    initial = next(u for u in first if u.text.startswith("Please explain me"))
    assert initial.session_hash == a1_hash
    assert initial.utterance_id.startswith(f"{ADAPTER_ID}-{a1_hash[:16]}-r0000b00s00-")
    assert initial.authorship_basis == "explicit_user_role+wrapper"
    assert initial.authorship_confidence == pytest.approx(0.9)
    assert initial.timestamp == _ts(1772359200000)

    feedback = next(u for u in first if u.text.startswith("I very like"))
    assert feedback.authorship_basis == "explicit_user_role+json_feedback"
    assert feedback.session_hash == a1_hash

    cleaned = next(u for u in first if "see below for file content" in u.text)
    assert cleaned.text_status is TextStatus.CLEANED
    for utterance in [*first, *second, *third]:
        if utterance is not cleaned:
            assert utterance.text_status is TextStatus.VERBATIM

    unknown_wrapper = next(u for u in first if "<placeholder>" in u.text)
    assert "unknown_wrapper" in unknown_wrapper.content_flags
    assert unknown_wrapper.session_hash == _session_hash(TASK_A2)

    # The nightly store has no history_item.json: pre-3.50 initial-message rules.
    nightly_initial = next(u for u in second if u.text.startswith("Today I written"))
    assert nightly_initial.authorship_confidence == pytest.approx(0.6)
    assert "possible_delegated_task" in nightly_initial.content_flags
    assert nightly_initial.session_hash == _session_hash(TASK_B1)
    nightly_feedback = next(u for u in second if u.text.startswith("It sounds"))
    assert nightly_feedback.authorship_confidence == pytest.approx(0.9)
    assert "possible_delegated_task" not in nightly_feedback.content_flags

    for record in outcome.records:
        assert adapter.verify(record, extracted[record.opaque_label]) == []

    # Re-extraction from the same snapshot is deterministic.
    record = next(r for r in outcome.records if r.opaque_label == "Roo Code 1")
    target = tmp_path / "snapshots" / record.instance_key[:12]
    again = list(adapter.extract(record, target))
    assert again == first


def test_malformed_variant_isolated(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.candidate_messages == 2
    assert record.estimated_records == 11
    assert record.schema_fingerprint == "g3-user-message+meta+ts"

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    texts = {u.text for u in extracted}
    assert texts == {
        "After the crash I lost my notes, can we restore them somehow?",
        "Looks well for me, thank you for the fast help.",
    }
    # The unreadable history_item.json falls back to the pre-3.50 rules.
    initial = next(u for u in extracted if u.text.startswith("After the crash"))
    assert initial.authorship_confidence == pytest.approx(0.6)
    assert "possible_delegated_task" in initial.content_flags

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.utterance_count == 2
    assert stats.malformed_tasks == 2
    assert stats.unsupported_tasks == 1
    assert stats.metadata_unreadable_tasks == 1
    assert adapter.verify(record, extracted) == []


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
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


def test_migration_mixed_generations_and_subtask_exclusion(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "mixed+meta+ts"
    assert record.candidate_messages == len(MIGRATION_TEXTS)
    assert record.earliest_timestamp == _ts(1732300800000)
    assert record.latest_timestamp == _ts(1769100200000)

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    texts = {u.text for u in extracted}
    assert texts == MIGRATION_TEXTS

    # Both delegated children are excluded: metadata path and argument match.
    assert "Refactor the FAKE config loader and report back." not in texts
    assert not any("[TASK RESUMPTION]" in text for text in texts)
    assert not any("The user denied this operation" in text for text in texts)
    assert not any("This wrapper never closes" in text for text in texts)

    # G1 records have no ts: the UI timestamp is the deterministic fallback.
    g1_initial = next(u for u in extracted if u.text.startswith("Please help me to translate"))
    assert g1_initial.timestamp == _ts(1732300800000)
    assert g1_initial.authorship_confidence == pytest.approx(0.6)
    assert "possible_delegated_task" in g1_initial.content_flags

    g2_initial = next(u for u in extracted if u.text.startswith("Check please"))
    assert g2_initial.authorship_confidence == pytest.approx(0.6)

    # The mixed resumed task has history metadata: full initial confidence, and
    # its ts-less G1 record takes the history_item timestamp.
    mixed_initial = next(u for u in extracted if u.text.startswith("Start the FAKE cleanup"))
    assert mixed_initial.authorship_confidence == pytest.approx(0.9)
    assert "possible_delegated_task" not in mixed_initial.content_flags
    assert mixed_initial.timestamp == _ts(1769100000000)
    mixed_resume = next(u for u in extracted if u.text.startswith("I returned back"))
    assert mixed_resume.timestamp == _ts(1769100200000)

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.excluded_subtasks == 2
    assert stats.unbalanced_wrappers == 1
    assert adapter.verify(record, extracted) == []


def test_oversized_history_skipped_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / ROO_EXT
    _write_min_task(root, "0195aaaa-0000-7000-8000-000000000001", 1772359200000, "Too big FAKE.")
    api_path = (
        root / "tasks" / "0195aaaa-0000-7000-8000-000000000001" / "api_conversation_history.json"
    )
    assert api_path.stat().st_size > 64

    opened: list[Path] = []
    original_open = Path.open
    original_read_bytes = Path.read_bytes

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return original_open(self, *args, **kwargs)

    def recording_read_bytes(self: Path) -> bytes:
        opened.append(self)
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    adapter = RooCodeAdapter(max_file_bytes=64)
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 0
    assert record.estimated_records == 0
    assert record.schema_fingerprint == "empty"

    target = tmp_path / "snap"
    capture = adapter.snapshot(record, root, target)
    assert [entry.relative_path for entry in capture.files] == ["roo-code-session-hashes.json"]
    assert list(adapter.extract(record, target)) == []
    assert api_path not in opened


def test_windows_appdata_roots(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    appdata = tmp_path / "Roaming"
    root = appdata / "Code" / "User" / "globalStorage" / ROO_EXT
    _write_min_task(root, "0195cccc-0000-7000-8000-000000000001", 1772359200000, "Windows FAKE.")
    context = DiscoveryContext(
        os_environment=OsEnvironment.WINDOWS,
        home=tmp_path / "home",
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ={"APPDATA": str(appdata)},
    )
    outcome = adapter.discover(context)
    assert len(outcome.records) == 1
    assert outcome.records[0].accessibility is Accessibility.FOUND
    assert outcome.records[0].candidate_messages == 1

    home2 = tmp_path / "home2"
    root2 = home2 / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / ROO_EXT
    _write_min_task(root2, "0195cccc-0000-7000-8000-000000000002", 1772359200000, "Fallback FAKE.")
    fallback_context = DiscoveryContext(
        os_environment=OsEnvironment.WINDOWS,
        home=home2,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ={},
    )
    fallback = adapter.discover(fallback_context)
    assert len(fallback.records) == 1
    assert fallback.records[0].accessibility is Accessibility.FOUND
    assert fallback.records[0].candidate_messages == 1


def test_wsl_discovery_and_host_store_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    server_root = home / ".vscode-server" / "data" / "User" / "globalStorage" / ROO_EXT
    _write_min_task(server_root, "0195dddd-0000-7000-8000-000000000001", 1772359200000, "WSL FAKE.")
    mount = tmp_path / "mnt"
    host_store = (
        mount
        / "c"
        / "Users"
        / "FAKE-user"
        / "AppData"
        / "Roaming"
        / "Code"
        / "User"
        / "globalStorage"
        / ROO_EXT
    )
    host_store.mkdir(parents=True)
    monkeypatch.setattr(roo_adapter_module, "_WSL_MOUNT_BASE", mount)

    adapter = RooCodeAdapter()
    context = DiscoveryContext(
        os_environment=OsEnvironment.WSL,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ={},
    )
    outcome = adapter.discover(context)
    found = [r for r in outcome.records if r.accessibility is Accessibility.FOUND]
    hints = [r for r in outcome.records if r.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT"]
    assert len(found) == 1
    assert found[0].candidate_messages == 1
    assert len(hints) == 1
    assert hints[0].accessibility is Accessibility.NOT_FOUND
    assert hints[0].stability is Stability.BETA
    assert hints[0].candidate_messages == 0
    assert hints[0].schema_fingerprint == "windows-host"


def test_variant_editor_roots_and_stability(tmp_path: Path) -> None:
    home = tmp_path / "home"
    base = home / "Library" / "Application Support"
    editor_roots = {
        "Code": base / "Code" / "User" / "globalStorage" / ROO_EXT,
        "Code - Insiders": base / "Code - Insiders" / "User" / "globalStorage" / ROO_EXT,
        "VSCodium": base / "VSCodium" / "User" / "globalStorage" / ROO_EXT,
        "code-server": home
        / ".local"
        / "share"
        / "code-server"
        / "User"
        / "globalStorage"
        / ROO_EXT,
    }
    for offset, root in enumerate(editor_roots.values()):
        _write_min_task(
            root,
            f"0195eeee-0000-7000-8000-00000000000{offset}",
            1772359200000 + offset * 60000,
            "Variant FAKE task text.",
        )
    unknown_root = base / "FooEditor" / "User" / "globalStorage" / ROO_EXT
    _write_min_task(unknown_root, "0195eeee-0000-7000-8000-000000000009", 1, "Unknown FAKE.")

    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(home))
    assert len(outcome.records) == 4
    discovered = set(outcome.instance_paths.values())
    assert unknown_root not in discovered
    for record in outcome.records:
        root = outcome.instance_paths[record.instance_key]
        expected = Stability.BETA if "VSCodium" in root.parts else Stability.STABLE
        assert record.stability is expected


def test_ui_cross_check_reports_missing_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / ROO_EXT
    task_id = "0195ffff-0000-7000-8000-000000000001"
    _write_min_task(root, task_id, 1772359200000, "Cross check FAKE text.")
    ui_payload = [
        {
            "ts": 1772359200000,
            "type": "say",
            "say": "user_feedback",
            "text": "Extra FAKE feedback that nobody typed.",
        }
    ]
    (root / "tasks" / task_id / "ui_messages.json").write_text(
        json.dumps(ui_payload), encoding="utf-8"
    )

    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.candidate_messages == 1
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    diagnostics = adapter.verify(record, extracted)
    assert any(
        d.code == "CARDINALITY_MISMATCH" and "ui_api_mismatch" in d.message for d in diagnostics
    )


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    record = outcome.records[0]
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    codes = {d.code for d in adapter.verify(record, duplicated)}
    assert "CARDINALITY_MISMATCH" in codes

    tampered = [
        extracted[0].model_copy(
            update={"text": 'leftover {"status": "denied"} <user_message> artifact'}
        ),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, tampered)}
    assert "SCHEMA_INVALID_VALUE" in codes

    stale = [
        extracted[0].model_copy(update={"timestamp": datetime(2015, 1, 1, tzinfo=UTC)}),
        *extracted[1:],
    ]
    codes = {d.code for d in adapter.verify(record, stale)}
    assert "SCHEMA_INVALID_VALUE" in codes


def test_never_opens_denylisted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []
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

    def failing_connect(*args: Any, **kwargs: Any) -> Any:
        msg = "sqlite3.connect must never be called by the roo_code adapter"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr(sqlite3, "connect", failing_connect)

    adapter = RooCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected task file reads to be recorded"
    allowed_names = {
        "api_conversation_history.json",
        "history_item.json",
        "ui_messages.json",
        "roo-code-session-hashes.json",
    }
    assert {path.name for path in opened} <= allowed_names
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not any(part in DENY_DIR_NAMES for part in path.parts), str(path)
    opened_names = {path.name for path in opened}
    assert "api_conversation_history.json" in opened_names
    assert "_index.json" not in opened_names
    assert "state.vscdb" not in opened_names
    assert "mcp_settings.json" not in opened_names
    assert "task_metadata.json" not in opened_names
    assert "claude_messages.json" not in opened_names
