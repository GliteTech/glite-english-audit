"""Fixture-driven tests for the Codex CLI source adapter."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.codex import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    NEVER_OPEN_NAMES,
    CodexAdapter,
    create_adapter,
)
from glite_english_audit.adapters.codex import (
    adapter as codex_adapter,
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
from glite_english_audit.verification.fixture_policy import load_fixture_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures" / "codex"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

INSTANCE_ONE_TEXTS = {
    "Hello, can you help me to write a small parser for the logs file?",
    "I very like this plan. Please continue with the second step.",
    "It give me error when I run the tests, what I am doing wrong?",
    "Please explain me how this authentication module works.",
    "I am agree that we should refactor it, but first write the tests.",
    "Thanks you, now it works much more better.",
    "How I can make this function more faster?",
    "Here is the screenshot, the button is not aligning good.",
}


def _context(
    home: Path,
    environ: dict[str, str] | None = None,
    os_environment: OsEnvironment = OsEnvironment.MACOS,
) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=os_environment, home=home, now=NOW, environ=environ or {}
    )


def _discover_success(monkeypatch: pytest.MonkeyPatch) -> DiscoveryOutcome:
    home = FIXTURES / "success" / "home"
    monkeypatch.setattr(codex_adapter, "_WSL_MOUNT_BASE", home / "mnt")
    return CodexAdapter().discover(_context(home, os_environment=OsEnvironment.WSL))


def _by_label(outcome: DiscoveryOutcome) -> dict[str, SourceInstanceRecord]:
    return {record.opaque_label: record for record in outcome.records}


def _snapshot_and_extract(
    outcome: DiscoveryOutcome, record: SourceInstanceRecord, target: Path
) -> list[NormalizedUtterance]:
    adapter = CodexAdapter()
    adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
    return list(adapter.extract(record, target))


def test_factory_and_identity() -> None:
    adapter = create_adapter()
    assert adapter.adapter_id == ADAPTER_ID == "codex"
    assert adapter.adapter_version == ADAPTER_VERSION
    assert adapter.stability is Stability.STABLE


def test_fixture_declarations_are_valid() -> None:
    for variant in ("success", "empty", "malformed", "unsupported", "migration"):
        meta = load_fixture_meta(FIXTURES / variant)
        assert meta.adapter_id == "codex"
        assert meta.synthetic is True


def test_discover_success_reports_two_labeled_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _discover_success(monkeypatch)
    assert len(outcome.records) == 2
    records = _by_label(outcome)
    first = records["Codex 1"]
    second = records["Codex 2"]

    home = FIXTURES / "success" / "home"
    assert outcome.instance_paths[first.instance_key] == home / ".codex"
    assert outcome.instance_paths[second.instance_key] == (
        home / "mnt" / "c" / "Users" / "fake-user" / ".codex"
    )

    assert first.accessibility is Accessibility.FOUND
    assert first.diagnostic_code is None
    assert first.candidate_messages == 8
    assert first.estimated_records == 11
    assert first.candidate_words > 20
    assert first.candidate_bytes > 100
    assert first.earliest_timestamp == datetime(2025, 12, 20, 8, 16, tzinfo=UTC)
    assert first.latest_timestamp == datetime(2026, 2, 3, 9, 32, tzinfo=UTC)
    assert first.app_version is not None and "0.146.2" in first.app_version
    assert first.schema_fingerprint == "legacy-events+paginated"
    expected_hash = hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()
    assert first.path_hash == expected_hash
    assert first.instance_key == expected_hash

    # The subagent session is counted pre-filter but contributes no candidates.
    assert second.accessibility is Accessibility.FOUND
    assert second.candidate_messages == 2
    assert second.estimated_records == 3
    assert second.earliest_timestamp == datetime(2026, 3, 1, 12, 0, 10, tzinfo=UTC)


def test_discover_records_carry_no_source_text(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _discover_success(monkeypatch)
    dumped = json.dumps([record.model_dump(mode="json") for record in outcome.records])
    for text in INSTANCE_ONE_TEXTS:
        assert text not in dumped
    assert "fake-user" not in dumped


def test_codex_home_override_wins(tmp_path: Path) -> None:
    codex_home = FIXTURES / "success" / "home" / ".codex"
    outcome = CodexAdapter().discover(_context(tmp_path, environ={"CODEX_HOME": str(codex_home)}))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.opaque_label == "Codex 1"
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 8


def test_missing_root_reports_not_found(tmp_path: Path) -> None:
    outcome = CodexAdapter().discover(
        _context(tmp_path, environ={"CODEX_HOME": str(tmp_path / "does-not-exist")})
    )
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.NOT_FOUND
    assert record.diagnostic_code == "SOURCE_NOT_FOUND"
    assert record.candidate_messages == 0
    assert record.estimated_records == 0


def test_discover_empty_fixture() -> None:
    outcome = CodexAdapter().discover(_context(FIXTURES / "empty" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.candidate_messages == 0
    assert record.estimated_records == 0
    assert record.earliest_timestamp is None
    assert record.schema_fingerprint == "empty+meta-only"


def test_discover_never_follows_a_symlink_into_the_session_tree(tmp_path: Path) -> None:
    """A link inside the date tree points out of the allowlisted root.

    Every other adapter skips symlinks during enumeration; this one has to as
    well, or the allowlist stops bounding what gets opened.
    """
    outside = tmp_path / "outside"
    day = outside / "2026" / "01" / "02"
    day.mkdir(parents=True)
    session_id = "0193a1b2-0000-7000-8000-000000000001"
    name = f"rollout-2026-01-02T09-00-00-{session_id}.jsonl"
    lines = [
        {
            "timestamp": "2026-01-02T09:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-01-02T09:00:00Z",
                "cli_version": "0.150.0",
                "cwd": "/home/fake-user/projects/site",
                "originator": "codex_cli_rs",
                "history_mode": "paginated",
            },
        },
        {
            "timestamp": "2026-01-02T09:01:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "kind": "plain", "message": "Please explain me."},
        },
    ]
    (day / name).write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    # One link at the year level and one at the file level: both are skipped.
    (sessions / "2026").symlink_to(outside / "2026", target_is_directory=True)
    linked_day = sessions / "2027" / "01" / "02"
    linked_day.mkdir(parents=True)
    (linked_day / name).symlink_to(day / name)

    outcome = CodexAdapter().discover(_context(home))

    record = outcome.records[0]
    assert record.candidate_messages == 0
    assert record.schema_fingerprint == "empty"


def test_discover_malformed_fixture() -> None:
    outcome = CodexAdapter().discover(_context(FIXTURES / "malformed" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.diagnostic_code is None
    assert record.candidate_messages == 2
    assert record.estimated_records == 2


def test_discover_unsupported_fixture() -> None:
    outcome = CodexAdapter().discover(_context(FIXTURES / "unsupported" / "home"))
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.candidate_messages == 0
    assert record.estimated_records == 0
    assert record.schema_fingerprint == "bare-items+unknown+unknown-history-mode"


def test_snapshot_copies_only_allowlisted_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = _discover_success(monkeypatch)
    record = _by_label(outcome)["Codex 1"]
    source_root = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = CodexAdapter().snapshot(record, source_root, target)

    expected = [
        "archived_sessions/rollout-2025-12-20T08-15-00-0193a1b2-0000-7000-8000-000000000003.jsonl",
        "sessions/2026/01/15/rollout-2026-01-15T10-00-00-"
        "0193a1b2-0000-7000-8000-000000000001.jsonl",
        "sessions/2026/02/03/rollout-2026-02-03T09-30-00-"
        "0193a1b2-0000-7000-8000-000000000002.jsonl",
    ]
    assert [entry.relative_path for entry in capture.files] == expected
    assert capture.snapshot_relative_dir == f"codex/{record.instance_key[:16]}"

    for entry in capture.files:
        source_bytes = (source_root / entry.relative_path).read_bytes()
        copied = target / entry.relative_path
        assert copied.read_bytes() == source_bytes
        assert entry.size_bytes == len(source_bytes)
        assert entry.sha256 == hashlib.sha256(source_bytes).hexdigest()
        if os.name == "posix":
            assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    copied_names = {path.name for path in target.rglob("*") if path.is_file()}
    assert not copied_names & set(NEVER_OPEN_NAMES)
    assert "notes.txt" not in copied_names


def test_extract_success_texts_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = _discover_success(monkeypatch)
    record = _by_label(outcome)["Codex 1"]
    utterances = _snapshot_and_extract(outcome, record, tmp_path / "snap")

    assert {utterance.text for utterance in utterances} == INSTANCE_ONE_TEXTS
    assert len(utterances) == record.candidate_messages
    assert len({utterance.utterance_id for utterance in utterances}) == len(utterances)
    for utterance in utterances:
        assert utterance.source_adapter == "codex"
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.modality is Modality.WRITTEN
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.source_path_hash == record.path_hash
        assert utterance.timestamp is not None
        assert utterance.content_flags == []
        assert utterance.utterance_id.startswith("codex-")

    by_text = {utterance.text: utterance for utterance in utterances}
    plain = by_text["I very like this plan. Please continue with the second step."]
    assert plain.authorship_confidence == 0.95
    assert "kind=plain" in plain.authorship_basis
    no_kind = by_text["Thanks you, now it works much more better."]
    assert no_kind.authorship_confidence == 0.9
    channel_b = by_text["How I can make this function more faster?"]
    assert channel_b.authorship_confidence == 0.85
    assert "response_item" in channel_b.authorship_basis
    assert channel_b.timestamp == datetime(2025, 12, 20, 8, 16, tzinfo=UTC)


def test_extract_is_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = _discover_success(monkeypatch)
    record = _by_label(outcome)["Codex 1"]
    first = _snapshot_and_extract(outcome, record, tmp_path / "snap-one")
    second = _snapshot_and_extract(outcome, record, tmp_path / "snap-two")
    assert first == second


def test_extract_migration_flags_forks_and_skips_compacted(tmp_path: Path) -> None:
    outcome = CodexAdapter().discover(_context(FIXTURES / "migration" / "home"))
    record = outcome.records[0]
    assert record.candidate_messages == 6
    assert record.estimated_records == 7
    utterances = _snapshot_and_extract(outcome, record, tmp_path / "snap")

    assert len(utterances) == 6
    assert all("COMPACTED-ONLY-TEXT" not in utterance.text for utterance in utterances)
    forked = [u for u in utterances if "forked_from" in u.content_flags]
    assert len(forked) == 3
    # The adapter keeps fork duplicates; the shared normalizer collapses them.
    repeated = [u for u in utterances if u.text == "We should deploy this on the staging first."]
    assert len(repeated) == 2
    assert len({u.session_hash for u in repeated}) == 2
    old_generation = [u for u in utterances if "Old but supported" in u.text]
    assert len(old_generation) == 1
    assert old_generation[0].authorship_confidence == 0.95


def test_extract_malformed_truncated_tail(tmp_path: Path) -> None:
    outcome = CodexAdapter().discover(_context(FIXTURES / "malformed" / "home"))
    record = outcome.records[0]
    utterances = _snapshot_and_extract(outcome, record, tmp_path / "snap")
    assert len(utterances) == 2
    assert {u.text for u in utterances} == {
        "My code keep crashing when I open the file.",
        "Sorry, I forget to save the file before running.",
    }
    for utterance in utterances:
        assert utterance.content_flags == ["truncated_tail"]
    assert all("SYNTHETIC" not in utterance.text for utterance in utterances)


def test_verify_clean_and_broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = _discover_success(monkeypatch)
    record = _by_label(outcome)["Codex 1"]
    utterances = _snapshot_and_extract(outcome, record, tmp_path / "snap")
    adapter = CodexAdapter()

    assert adapter.verify(record, utterances) == []

    duplicated = adapter.verify(record, [*utterances, utterances[0]])
    assert any(
        diagnostic.code == "CARDINALITY_MISMATCH" and "emitted 2 times" in diagnostic.message
        for diagnostic in duplicated
    )

    blank = utterances[0].model_copy(update={"text": "   "})
    blank_diagnostics = adapter.verify(record, [blank, *utterances[1:]])
    assert any(diagnostic.code == "SCHEMA_INVALID_VALUE" for diagnostic in blank_diagnostics)

    stale = utterances[0].model_copy(update={"timestamp": datetime(2001, 1, 1, tzinfo=UTC)})
    stale_diagnostics = adapter.verify(record, [stale, *utterances[1:]])
    assert any(
        diagnostic.code == "SCHEMA_INVALID_VALUE" and "range" in diagnostic.message
        for diagnostic in stale_diagnostics
    )

    short = adapter.verify(record, utterances[:-1])
    assert any(
        diagnostic.code == "CARDINALITY_MISMATCH" and "discovery counted" in diagnostic.message
        for diagnostic in short
    )


def test_never_opens_denylisted_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = FIXTURES / "success" / "home"
    accessed: list[Path] = []
    real_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        accessed.append(self)
        return real_open(self, *args, **kwargs)

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        accessed.append(self)
        return real_read_text(self, *args, **kwargs)

    def spy_read_bytes(self: Path) -> bytes:
        accessed.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    outcome = _discover_success(monkeypatch)
    adapter = CodexAdapter()
    for index, record in enumerate(outcome.records):
        target = tmp_path / f"snap-{index}"
        adapter.snapshot(record, outcome.instance_paths[record.instance_key], target)
        list(adapter.extract(record, target))

    assert accessed, "the spy must have recorded adapter file access"
    opened_names = {path.name for path in accessed}
    assert not opened_names & set(NEVER_OPEN_NAMES)
    codex_roots = (
        home / ".codex",
        home / "mnt" / "c" / "Users" / "fake-user" / ".codex",
    )
    for path in accessed:
        for root in codex_roots:
            if path.is_relative_to(root):
                assert path.name.startswith("rollout-")
                assert path.suffix == ".jsonl"
