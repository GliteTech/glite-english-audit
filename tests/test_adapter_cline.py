"""Fixture-driven tests for the Cline source adapter."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import glite_english_audit.adapters.cline.adapter as cline_adapter_module
from glite_english_audit.adapters.cline import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ClineAdapter,
    create_adapter,
)
from glite_english_audit.adapters.cline.adapter import DENY_DIR_NAMES, DENY_FILE_NAMES
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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cline"
EXTENSION_DIR = "saoudrizwan.claude-dev"

TASK_1 = "1748772000000"
TASK_2 = "1748858400000"
SESSION_G3 = "sess-fake-0001"
TASK_LEGACY_PARENT = "1717243800000"
TASK_LEGACY_SPAWNED = "1717244000000"

FAMILY_A_TEXTS = {
    "Please help me to set up the deploy pipeline, I am not understand the current config.",
    "Yes, we should to keep the old script for now.",
    "I very like this plan, please continue in same way.",
    "I returned back to this task, let's finish it today.",
    "Do it more careful please.",
    "<future-tag>note</future-tag> Also the tests is failing on my machine.",
    "The file 'src/app.py' (see below for file content) has a bug I cannot found.",
    "Since many years I prefer the simple solutions.",
}
FAMILY_B_TEXTS = {
    "How I can make this migration more safe?",
    "I am agree with your suggestion, let's proceed.",
    "Also please explain me why the cache is not worked.",
    "Undated but real question, how you would name this function?",
}


def _context(
    home: Path,
    os_environment: OsEnvironment = OsEnvironment.MACOS,
    environ: dict[str, str] | None = None,
) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=os_environment,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _session_hash(unit_id: str) -> str:
    return hashlib.sha256(unit_id.encode("utf-8")).hexdigest()


def _extract_by_label(
    adapter: ClineAdapter, outcome: DiscoveryOutcome, tmp_path: Path
) -> dict[str, list[NormalizedUtterance]]:
    extracted: dict[str, list[NormalizedUtterance]] = {}
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / "snapshots" / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        extracted[record.opaque_label] = list(adapter.extract(record, target))
    return extracted


def _write_minimal_task(root: Path, task_id: str, text: str) -> None:
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps([{"role": "user", "content": f"<task>\n{text}\n</task>"}]),
        encoding="utf-8",
    )


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
    assert adapter.stability is Stability.BETA


def test_discover_success_two_instances() -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    assert len(outcome.records) == 2
    by_label = {record.opaque_label: record for record in outcome.records}
    assert set(by_label) == {"Cline 1", "Cline 2"}

    first = by_label["Cline 1"]
    second = by_label["Cline 2"]
    for record in (first, second):
        assert record.accessibility is Accessibility.FOUND
        assert record.storage_format == "json"
        assert record.diagnostic_code is None
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    # Family A (June activity) labels instance 1; family B (July) instance 2.
    assert first.schema_fingerprint == "g2-task-store+state-index"
    assert second.schema_fingerprint == "g3-sdk-sessions+state-index+db"
    assert first.candidate_messages == len(FAMILY_A_TEXTS)
    assert second.candidate_messages == len(FAMILY_B_TEXTS)
    assert first.estimated_records == 13
    assert second.estimated_records == 10
    assert first.candidate_words == sum(count_words(text) for text in FAMILY_A_TEXTS)
    assert second.candidate_words == sum(count_words(text) for text in FAMILY_B_TEXTS)
    assert first.candidate_bytes == sum(len(text.encode("utf-8")) for text in FAMILY_A_TEXTS)
    assert first.earliest_timestamp == datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    assert first.latest_timestamp == datetime(2025, 6, 2, 10, 0, tzinfo=UTC)
    assert second.earliest_timestamp == datetime(2025, 7, 1, 9, 0, tzinfo=UTC)
    assert second.latest_timestamp == datetime(2025, 7, 1, 9, 5, tzinfo=UTC)
    assert adapter.discovery_diagnostics() == []


def test_discover_not_found_and_found_empty(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    missing = adapter.discover(_context(tmp_path / "nobody-home"))
    assert len(missing.records) == 1
    assert missing.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing.records[0].diagnostic_code == "SOURCE_NOT_FOUND"

    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    assert len(outcome.records) == 2
    for record in outcome.records:
        assert record.accessibility is Accessibility.FOUND
        assert record.schema_fingerprint == "empty"
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0
        assert record.earliest_timestamp is None


def test_discover_editor_variants_linux(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for editor, task_id in (
        ("Code", "1748772000000"),
        ("Code - Insiders", "1748772060000"),
        ("VSCodium", "1748772120000"),
    ):
        root = home / ".config" / editor / "User" / "globalStorage" / EXTENSION_DIR
        _write_minimal_task(root, task_id, "One sentence for the editor variant test.")
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(home, os_environment=OsEnvironment.LINUX))
    assert len(outcome.records) == 3
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)
    assert {record.opaque_label for record in outcome.records} == {
        "Cline 1",
        "Cline 2",
        "Cline 3",
    }
    roots = {path.parent.parent.parent.name for path in outcome.instance_paths.values()}
    assert roots == {"Code", "Code - Insiders", "VSCodium"}


def test_discover_wsl_server_path_and_mount_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "wsl-home"
    server_root = home / ".vscode-server" / "data" / "User" / "globalStorage" / EXTENSION_DIR
    _write_minimal_task(server_root, "1748772000000", "A sentence in the WSL server store.")
    _write_minimal_task(home / ".cline" / "data", "1748772060000", "A sentence in family B.")
    mount = tmp_path / "mnt"
    (mount / "c" / "Users" / "fakeuser" / ".cline" / "data").mkdir(parents=True)
    monkeypatch.setattr(cline_adapter_module, "_WSL_MOUNT_BASE", mount)

    adapter = ClineAdapter()
    outcome = adapter.discover(_context(home, os_environment=OsEnvironment.WSL))
    assert len(outcome.records) == 2
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)
    for path in outcome.instance_paths.values():
        assert not str(path).startswith(str(mount))
    codes = [diagnostic.code for diagnostic in adapter.discovery_diagnostics()]
    assert codes == ["SOURCE_WSL_HOST_STORE_HINT"]

    # Outside WSL the mount is ignored and the server path is not probed.
    linux_outcome = adapter.discover(_context(home, os_environment=OsEnvironment.LINUX))
    assert adapter.discovery_diagnostics() == []
    assert len(linux_outcome.records) == 1
    assert not any(".vscode-server" in path.parts for path in linux_outcome.instance_paths.values())


def test_discover_windows_appdata_and_userprofile(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    profile = tmp_path / "profile"
    code_root = appdata / "Code" / "User" / "globalStorage" / EXTENSION_DIR
    _write_minimal_task(code_root, "1748772000000", "A sentence in the Windows editor store.")
    _write_minimal_task(profile / ".cline" / "data", "1748772060000", "A sentence in family B.")
    adapter = ClineAdapter()
    outcome = adapter.discover(
        _context(
            tmp_path / "unused-home",
            os_environment=OsEnvironment.WINDOWS,
            environ={"APPDATA": str(appdata), "USERPROFILE": str(profile)},
        )
    )
    assert len(outcome.records) == 2
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)


def test_env_overrides_select_family_b_root(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    data_dir = str(FIXTURES / "success" / "home" / ".cline" / "data")
    outcome = adapter.discover(_context(tmp_path / "home-a", environ={"CLINE_DATA_DIR": data_dir}))
    assert len(outcome.records) == 1
    assert outcome.records[0].accessibility is Accessibility.FOUND
    assert outcome.records[0].candidate_messages == len(FAMILY_B_TEXTS)

    cline_dir = str(FIXTURES / "success" / "home" / ".cline")
    outcome = adapter.discover(_context(tmp_path / "home-b", environ={"CLINE_DIR": cline_dir}))
    assert len(outcome.records) == 1
    assert outcome.records[0].candidate_messages == len(FAMILY_B_TEXTS)


def test_env_override_missing_dir_never_falls_back(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_minimal_task(home / ".cline" / "data", "1748772000000", "Default store sentence.")
    adapter = ClineAdapter()
    override = str(tmp_path / "does-not-exist")
    outcome = adapter.discover(_context(home, environ={"CLINE_DATA_DIR": override}))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.NOT_FOUND
    assert record.diagnostic_code == "SOURCE_NOT_FOUND"
    assert outcome.instance_paths[record.instance_key] == Path(override)


def test_snapshot_copies_allowlisted_files_byte_for_byte(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Cline 1")
    source_dir = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source_dir, target)

    expected = {
        f"tasks/{TASK_1}/api_conversation_history.json",
        f"tasks/{TASK_1}/ui_messages.json",
        f"tasks/{TASK_2}/api_conversation_history.json",
        "cline-source-paths.json",
    }
    assert {entry.relative_path for entry in capture.files} == expected
    api_entry = next(
        entry
        for entry in capture.files
        if entry.relative_path == f"tasks/{TASK_1}/api_conversation_history.json"
    )
    source_bytes = (source_dir / "tasks" / TASK_1 / "api_conversation_history.json").read_bytes()
    assert api_entry.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert api_entry.size_bytes == len(source_bytes)
    assert (target / "tasks" / TASK_1 / "api_conversation_history.json").read_bytes() == (
        source_bytes
    )
    copied_names = {path.name for path in target.rglob("*") if path.is_file()}
    assert not copied_names & DENY_FILE_NAMES
    assert not any("checkpoints" in path.parts for path in target.rglob("*"))


def test_extract_success_texts_statuses_and_determinism(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    extracted = _extract_by_label(adapter, outcome, tmp_path)

    first = extracted["Cline 1"]
    second = extracted["Cline 2"]
    assert {u.text for u in first} == FAMILY_A_TEXTS
    assert {u.text for u in second} == FAMILY_B_TEXTS

    for utterance in [*first, *second]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")

    task_1_hash = _session_hash(TASK_1)
    liked = next(u for u in first if u.text.startswith("I very like"))
    assert liked.utterance_id == f"{ADAPTER_ID}-{task_1_hash[:16]}-r0004-s00"
    assert liked.session_hash == task_1_hash
    assert liked.timestamp == datetime(2025, 6, 1, 10, 3, tzinfo=UTC)
    assert liked.text_status is TextStatus.VERBATIM
    assert liked.authorship_basis == "explicit_user_role+wrapper"
    assert liked.authorship_confidence == pytest.approx(0.9)

    cleaned = next(u for u in first if u.text.startswith("The file"))
    assert cleaned.text_status is TextStatus.CLEANED
    assert cleaned.session_hash == _session_hash(TASK_2)
    # No UI stream in task 2: the taskId epoch-ms fallback dates the span.
    assert cleaned.timestamp == datetime(2025, 6, 2, 10, 0, tzinfo=UTC)

    unknown_tag = next(u for u in first if u.text.startswith("<future-tag>"))
    assert "unknown_wrapper" in unknown_tag.content_flags
    assert unknown_tag.timestamp == datetime(2025, 6, 1, 10, 6, tzinfo=UTC)

    session_hash = _session_hash(SESSION_G3)
    assert {u.session_hash for u in second} == {session_hash}
    wrapped = next(u for u in second if u.text.startswith("How I can"))
    assert wrapped.utterance_id == f"{ADAPTER_ID}-{session_hash[:16]}-msg-0001"
    assert wrapped.text_status is TextStatus.VERBATIM
    assert wrapped.authorship_basis == "explicit_user_role+user_input"
    assert wrapped.authorship_confidence == pytest.approx(0.9)
    assert wrapped.timestamp == datetime(2025, 7, 1, 9, 0, tzinfo=UTC)

    unwrapped = next(u for u in second if u.text.startswith("Also please explain"))
    assert unwrapped.text_status is TextStatus.UNKNOWN
    assert "unwrapped_user_text" in unwrapped.content_flags
    assert unwrapped.authorship_confidence == pytest.approx(0.7)

    undated = next(u for u in second if u.text.startswith("Undated"))
    assert undated.utterance_id == f"{ADAPTER_ID}-{session_hash[:16]}-msg-0008"
    # No message ts: the session manifest started_at dates the utterance.
    assert undated.timestamp == datetime(2025, 7, 1, 9, 0, tzinfo=UTC)

    # The duplicated user_input (msg-0009) collapsed onto the earliest copy.
    agreed = [u for u in second if u.text.startswith("I am agree")]
    assert len(agreed) == 1
    assert agreed[0].utterance_id == f"{ADAPTER_ID}-{session_hash[:16]}-msg-0005"

    record = next(r for r in outcome.records if r.opaque_label == "Cline 2")
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.utterance_count == len(FAMILY_B_TEXTS)
    assert dict(stats.counter_totals) == {
        "mixed_user_message": 1,
        "non_interactive_session": 1,
        "non_lead_messages_file": 1,
        "non_lead_session": 1,
        "runtime_composed_prefix": 1,
        "user_command_message": 1,
    }
    assert dict(stats.unit_statuses) == {
        "sessions/sess-fake-0001": "supported",
        "sessions/sess-fake-0002": "excluded",
        "sessions/sess-fake-0003": "excluded",
    }

    # Re-extraction from the same snapshot is deterministic.
    target = (
        tmp_path
        / "snapshots"
        / next(r.instance_key[:12] for r in outcome.records if r.opaque_label == "Cline 1")
    )
    again = list(
        adapter.extract(next(r for r in outcome.records if r.opaque_label == "Cline 1"), target)
    )
    assert again == first


def test_extract_success_family_a_counters(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Cline 1")
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.units_scanned == 2
    assert dict(stats.counter_totals) == {"unbalanced_wrapper": 1}
    assert adapter.verify(record, extracted) == []


def test_malformed_units_fail_closed_but_instance_survives(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 1
    assert record.estimated_records == 2

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert [u.text for u in extracted] == ["This sentence is fine, please check my grammar here."]
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert dict(stats.unit_statuses) == {
        "tasks/1750000000000": "supported",
        "tasks/1750000000001": "malformed_file",
        "tasks/1750000000002": "malformed_file",
        "tasks/1750000000003": "malformed_file",
        "sessions/sess-fake-v2000": "unsupported_schema",
    }
    assert adapter.verify(record, extracted) == []


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "unsupported" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.schema_fingerprint == "unsupported"
    assert record.candidate_messages == 0
    assert record.candidate_words == 0

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert extracted == []
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert dict(stats.unit_statuses) == {
        "tasks/1751000000000": "ui_only_task",
        "tasks/1751000000001": "unsupported_schema",
    }
    assert adapter.verify(record, extracted) == []


def test_migration_legacy_ui_name_and_new_task_exclusion(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "g1-claude-messages"
    assert record.candidate_messages == 3

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    texts = {u.text for u in extracted}
    assert texts == {
        "Long time ago I started this refactor and never finished it.",
        "Please explain me what you did in the subtask.",
        "I checked it already, all works good now.",
    }
    assert not any("model-composed" in text for text in texts)

    parent_hash = _session_hash(TASK_LEGACY_PARENT)
    spawned_hash = _session_hash(TASK_LEGACY_SPAWNED)
    initial = next(u for u in extracted if u.text.startswith("Long time ago"))
    assert initial.session_hash == parent_hash
    # Legacy claude_messages.json supplies the structure-only timestamps.
    assert initial.timestamp == datetime.fromtimestamp(1717243800, tz=UTC)
    feedback = next(u for u in extracted if u.text.startswith("Please explain me"))
    assert feedback.timestamp == datetime.fromtimestamp(1717243980, tz=UTC)
    answer = next(u for u in extracted if u.text.startswith("I checked it"))
    assert answer.session_hash == spawned_hash
    assert answer.timestamp == datetime.fromtimestamp(1717244100, tz=UTC)

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert dict(stats.counter_totals) == {"subtask_initial_message": 1}
    assert adapter.verify(record, extracted) == []


def test_channel_mismatch_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".config" / "Code" / "User" / "globalStorage" / EXTENSION_DIR
    task_dir = root / "tasks" / "1748772000000"
    task_dir.mkdir(parents=True)
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps([{"role": "user", "content": "<task>\nText A here today.\n</task>"}]),
        encoding="utf-8",
    )
    (task_dir / "ui_messages.json").write_text(
        json.dumps(
            [{"ts": 1748772000000, "type": "say", "say": "task", "text": "Different text."}]
        ),
        encoding="utf-8",
    )
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(home, os_environment=OsEnvironment.LINUX))
    extracted = _extract_by_label(adapter, outcome, tmp_path)["Cline 1"]
    assert len(extracted) == 1
    assert extracted[0].text == "Text A here today."
    assert "channel_mismatch" in extracted[0].content_flags


def test_oversized_file_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    root = home / ".cline" / "data"
    _write_minimal_task(root, "1748772000000", "Ok.")
    big_dir = root / "tasks" / "1748772060000"
    big_dir.mkdir(parents=True)
    (big_dir / "api_conversation_history.json").write_text(
        json.dumps([{"role": "user", "content": "<task>\n" + "word " * 200 + "\n</task>"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cline_adapter_module, "MAX_CONVERSATION_FILE_BYTES", 100)
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(home))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 1

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert [u.text for u in extracted] == ["Ok."]
    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert dict(stats.unit_statuses) == {
        "tasks/1748772000000": "supported",
        "tasks/1748772060000": "oversized_file",
    }


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Cline 2")
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    codes = {d.code for d in adapter.verify(record, duplicated)}
    assert "CARDINALITY_MISMATCH" in codes

    tampered = [
        extracted[0].model_copy(update={"text": 'leftover <user_input mode="act">tag'}),
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

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    adapter = ClineAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected conversation reads to be recorded"
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not any(part in DENY_DIR_NAMES for part in path.parts), str(path)
        assert not path.name.startswith("remote_config_"), str(path)
        assert not path.name.startswith("state.vscdb"), str(path)
    opened_names = {path.name for path in opened}
    assert "api_conversation_history.json" in opened_names
    assert "ui_messages.json" in opened_names
    assert f"{SESSION_G3}.messages.json" in opened_names
