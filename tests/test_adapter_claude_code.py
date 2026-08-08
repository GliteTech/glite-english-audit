"""Fixture-driven tests for the Claude Code source adapter."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from glite_english_audit.adapters.claude_code import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ClaudeCodeAdapter,
    create_adapter,
)
from glite_english_audit.adapters.claude_code.adapter import DENY_DIR_NAMES, DENY_FILE_NAMES
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

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "claude_code"

SESSION_1 = "11111111-1111-4111-8111-111111111111"
SESSION_2 = "22222222-2222-4222-8222-222222222222"
SESSION_LEGACY_A = "88888888-8888-4888-8888-888888888888"
SESSION_LEGACY_B = "99999999-9999-4999-8999-999999999999"

PROJECT_1_TEXTS = {
    "Please explain me how the deploy script works, I am not sure I understand it.",
    "I very like this plan, let's do it this way.",
    "How I can make this test more stable?",
    "I did not received any error, but the page is still empty. What we should check first?",
}
PROJECT_2_TEXTS = {
    "Today I written a long note about my learning progress.",
    "Can you help me to improve this paragraph? It sounds not natural for me.",
    "I am agree that the second variant reads better.",
    "Since many years I want to write more clear emails in English.",
    "<future-widget>hello</future-widget> Also please check the grammar here.",
}


def _context(home: Path, environ: dict[str, str] | None = None) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ or {},
    )


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _extract_by_label(
    adapter: ClaudeCodeAdapter, outcome: DiscoveryOutcome, tmp_path: Path
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
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    assert len(outcome.records) == 2
    by_label = {record.opaque_label: record for record in outcome.records}
    assert set(by_label) == {"Claude Code 1", "Claude Code 2"}

    first = by_label["Claude Code 1"]
    second = by_label["Claude Code 2"]
    for record in (first, second):
        assert record.accessibility is Accessibility.FOUND
        assert record.schema_fingerprint == "v2-current"
        assert record.storage_format == "jsonl"
        assert record.app_version == "2.1.210"
        assert record.diagnostic_code is None
        assert record.instance_key == record.path_hash
        assert outcome.instance_paths[record.instance_key].is_dir()

    # Earliest activity (June) labels instance 1; July labels instance 2.
    assert first.candidate_messages == len(PROJECT_1_TEXTS)
    assert second.candidate_messages == len(PROJECT_2_TEXTS)
    assert first.estimated_records == 16
    assert second.estimated_records == 6
    assert first.candidate_words == sum(count_words(text) for text in PROJECT_1_TEXTS)
    assert second.candidate_words == sum(count_words(text) for text in PROJECT_2_TEXTS)
    assert first.candidate_bytes == sum(len(text.encode("utf-8")) for text in PROJECT_1_TEXTS)
    assert first.earliest_timestamp == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert first.latest_timestamp == datetime(2026, 6, 1, 10, 40, tzinfo=UTC)


def test_discover_not_found_and_found_empty(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()

    missing = adapter.discover(_context(tmp_path / "nobody-home"))
    assert len(missing.records) == 1
    assert missing.records[0].accessibility is Accessibility.NOT_FOUND
    assert missing.records[0].diagnostic_code == "SOURCE_NOT_FOUND"

    (tmp_path / "installed" / ".claude").mkdir(parents=True)
    installed = adapter.discover(_context(tmp_path / "installed"))
    assert len(installed.records) == 1
    assert installed.records[0].accessibility is Accessibility.FOUND
    assert installed.records[0].diagnostic_code is None
    assert installed.records[0].candidate_messages == 0

    (tmp_path / "installed" / ".claude" / "projects").mkdir()
    with_projects = adapter.discover(_context(tmp_path / "installed"))
    assert with_projects.records[0].accessibility is Accessibility.FOUND
    assert with_projects.records[0].candidate_messages == 0


def test_discover_empty_store_fixture() -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "empty" / "home"))
    assert len(outcome.records) == 2
    for record in outcome.records:
        assert record.accessibility is Accessibility.FOUND
        assert record.candidate_messages == 0
        assert record.candidate_words == 0
        assert record.estimated_records == 0
        assert record.earliest_timestamp is None
        assert record.schema_fingerprint == "empty"


def test_config_dir_override(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    override = str(FIXTURES / "success" / "home" / ".claude")
    outcome = adapter.discover(_context(tmp_path, environ={"CLAUDE_CONFIG_DIR": override}))
    assert len(outcome.records) == 2
    assert all(record.accessibility is Accessibility.FOUND for record in outcome.records)

    without_override = adapter.discover(_context(tmp_path))
    assert without_override.records[0].accessibility is Accessibility.NOT_FOUND


def test_snapshot_copies_transcripts_byte_for_byte(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Claude Code 1")
    source_dir = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"
    capture = adapter.snapshot(record, source_dir, target)

    transcript_entries = [e for e in capture.files if e.relative_path.endswith(".jsonl")]
    assert [e.relative_path for e in transcript_entries] == [f"{SESSION_1}.jsonl"]
    source_bytes = (source_dir / f"{SESSION_1}.jsonl").read_bytes()
    assert transcript_entries[0].sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert transcript_entries[0].size_bytes == len(source_bytes)
    assert (target / f"{SESSION_1}.jsonl").read_bytes() == source_bytes

    copied = sorted(p.name for p in target.rglob("*") if p.is_file())
    assert copied == sorted(e.relative_path for e in capture.files)
    assert not any("subagents" in p.parts for p in target.rglob("*"))


def test_extract_success_texts_flags_and_determinism(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    extracted = _extract_by_label(adapter, outcome, tmp_path)

    first = extracted["Claude Code 1"]
    second = extracted["Claude Code 2"]
    assert {u.text for u in first} == PROJECT_1_TEXTS
    assert {u.text for u in second} == PROJECT_2_TEXTS

    for utterance in [*first, *second]:
        assert utterance.modality is Modality.WRITTEN
        assert utterance.text_status is TextStatus.VERBATIM
        assert utterance.source_adapter == ADAPTER_ID
        assert utterance.adapter_version == ADAPTER_VERSION
        assert utterance.utterance_id.startswith(f"{ADAPTER_ID}-")

    session_1_hash = _session_hash(SESSION_1)
    typed = next(u for u in first if u.text.startswith("I very like"))
    assert typed.utterance_id == f"{ADAPTER_ID}-{session_1_hash[:16]}-u3"
    assert typed.session_hash == session_1_hash
    assert typed.authorship_basis == "explicit_user_role+origin_human"
    assert typed.authorship_confidence == pytest.approx(0.95)

    plain = next(u for u in first if u.text.startswith("Please explain me"))
    assert plain.authorship_basis == "explicit_user_role"
    assert plain.authorship_confidence == pytest.approx(0.9)

    mismatched = next(u for u in second if u.text.startswith("Since many years"))
    assert "session_id_mismatch" in mismatched.content_flags
    unknown_wrapper = next(u for u in second if u.text.startswith("<future-widget>"))
    assert "unknown_wrapper" in unknown_wrapper.content_flags

    # Re-extraction from the same snapshot is deterministic.
    target = (
        tmp_path
        / "snapshots"
        / next(r.instance_key[:12] for r in outcome.records if r.opaque_label == "Claude Code 1")
    )
    again = list(
        adapter.extract(
            next(r for r in outcome.records if r.opaque_label == "Claude Code 1"), target
        )
    )
    assert again == first


def test_malformed_file_thresholds(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "malformed" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.candidate_messages == 10

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert len(extracted) == 10
    assert all("unsupported file" not in u.text for u in extracted)
    tail_flagged = [u for u in extracted if "truncated_tail_dropped" in u.content_flags]
    assert len(tail_flagged) == 1
    assert tail_flagged[0].text.startswith("In the end everything worked")

    stats = adapter.extraction_stats(record.instance_key)
    assert stats is not None
    assert stats.utterance_count == 10
    assert stats.truncated_tail_files == ("44444444-4444-4444-8444-444444444444.jsonl",)
    assert stats.unsupported_files == ("55555555-5555-4555-8555-555555555555.jsonl",)

    assert adapter.verify(record, extracted) == []


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "unsupported" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.UNSUPPORTED_SCHEMA
    assert record.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
    assert record.candidate_messages == 0
    assert record.candidate_words == 0
    assert record.schema_fingerprint == "unsupported"

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert extracted == []
    assert adapter.verify(record, extracted) == []


def test_migration_fork_dedup_and_legacy_fingerprint(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "migration" / "home"))
    assert len(outcome.records) == 1
    record = outcome.records[0]
    assert record.accessibility is Accessibility.FOUND
    assert record.schema_fingerprint == "v1-legacy"
    assert record.candidate_messages == 3

    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert len(extracted) == 3
    by_uuid = {u.utterance_id.split("-", 2)[-1]: u for u in extracted}
    assert set(by_uuid) == {"la-01", "la-07", "lb-08"}

    hash_a = _session_hash(SESSION_LEGACY_A)
    hash_b = _session_hash(SESSION_LEGACY_B)
    # The earliest (original) file wins canonical attribution for the fork.
    assert by_uuid["la-01"].session_hash == hash_a
    assert by_uuid["la-07"].session_hash == hash_a
    assert by_uuid["lb-08"].session_hash == hash_b

    texts = {u.text for u in extracted}
    assert not any("<bash-" in text for text in texts)
    assert not any("sidechain" in text.lower() for text in texts)
    assert not any("synthetic session summary" in text for text in texts)
    assert adapter.verify(record, extracted) == []


def test_verify_reports_structural_problems(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    record = next(r for r in outcome.records if r.opaque_label == "Claude Code 2")
    extracted = _extract_by_label(adapter, outcome, tmp_path)[record.opaque_label]
    assert adapter.verify(record, extracted) == []

    duplicated = [*extracted, extracted[0]]
    codes = {d.code for d in adapter.verify(record, duplicated)}
    assert "CARDINALITY_MISMATCH" in codes

    tampered = [
        extracted[0].model_copy(update={"text": "leftover <system-reminder>tag</system-reminder>"}),
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

    adapter = ClaudeCodeAdapter()
    outcome = adapter.discover(_context(FIXTURES / "success" / "home"))
    for record in outcome.records:
        source = outcome.instance_paths[record.instance_key]
        target = tmp_path / record.instance_key[:12]
        adapter.snapshot(record, source, target)
        list(adapter.extract(record, target))

    assert opened, "expected transcript reads to be recorded"
    for path in opened:
        assert path.name not in DENY_FILE_NAMES, str(path)
        assert not any(part in DENY_DIR_NAMES for part in path.parts), str(path)
        assert not path.name.endswith(".meta.json"), str(path)
    opened_names = {path.name for path in opened}
    assert f"{SESSION_1}.jsonl" in opened_names
    assert f"{SESSION_2}.jsonl" in opened_names
