"""Tests for the calibration profile models and loader."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.io import read_jsonl_models, write_jsonl_models
from glite_english_audit.estimation.profile import (
    PROFILE_SCHEMA_VERSION,
    CalibrationRecord,
    TokenUsageProfile,
    TokenUsageProfileEntry,
    default_profile_path,
    load_token_usage_profile,
)

EXPECTED_STEPS = {"find-mistakes", "verify-findings", "create-safe-records"}
EXPECTED_RUNTIMES = {"claude-code", "codex"}


def _entry(**overrides: object) -> TokenUsageProfileEntry:
    fields: dict[str, object] = {
        "step": "find-mistakes",
        "runtime": "claude-code",
        "model": "pinned-model-id",
        "effort": "medium",
        "messages_measured": 500,
        "average_words_per_message": 42.0,
        "fixed_input_tokens_per_batch": 1850,
        "input_tokens_per_message": 96.0,
        "input_tokens_per_word": 2.29,
        "cached_input_tokens_per_message": 80.0,
        "output_tokens_per_message": 31.0,
        "retry_rate": 0.04,
        "p50_total_tokens_per_message": 118.0,
        "p90_total_tokens_per_message": 176.0,
    }
    fields.update(overrides)
    return TokenUsageProfileEntry.model_validate(fields)


def _record(**overrides: object) -> CalibrationRecord:
    fields: dict[str, object] = {
        "runtime": "claude-code",
        "model": "pinned-model-id",
        "effort": "medium",
        "step": "find-mistakes",
        "skill_version": 1,
        "prompt_version": 1,
        "schema_version": 1,
        "batch_size": 25,
        "words": 1000,
        "utterances": 25,
        "fresh_input_tokens": 2000,
        "cached_input_tokens": 300,
        "output_tokens": 200,
        "retries": 0,
        "duration_seconds": 30.0,
        "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return CalibrationRecord.model_validate(fields)


def test_committed_profile_loads_and_covers_all_cells() -> None:
    profile = load_token_usage_profile()
    assert profile.schema_version == PROFILE_SCHEMA_VERSION
    cells = {(entry.step, entry.runtime) for entry in profile.entries}
    assert cells == {(step, runtime) for step in EXPECTED_STEPS for runtime in EXPECTED_RUNTIMES}


def test_committed_profile_calibration_state() -> None:
    # Claude Code cells were measured on real owner data (2026-08-08);
    # Codex cells stay uncalibrated until a calibration run under Codex.
    profile = load_token_usage_profile()
    for entry in profile.entries:
        if entry.runtime == "claude-code":
            assert entry.model == "claude-fable-5"
            assert entry.messages_measured > 0
            assert not entry.is_uncalibrated
            assert entry.p90_total_tokens_per_message >= entry.p50_total_tokens_per_message
        else:
            assert entry.model == "uncalibrated-default"
            assert entry.messages_measured == 0
            assert entry.is_uncalibrated
    assert [e for e in profile.low_confidence_entries() if e.runtime == "codex"]


def test_default_profile_path_points_into_calibration_dir() -> None:
    path = default_profile_path()
    assert path.name == "token-usage-profile.json"
    assert path.parent.name == "calibration"
    assert path.is_file()


def test_loader_reads_custom_path(tmp_path: Path) -> None:
    profile = TokenUsageProfile(schema_version=1, entries=(_entry(),))
    target = tmp_path / "profile.json"
    target.write_text(profile.model_dump_json(), encoding="utf-8")
    loaded = load_token_usage_profile(target)
    assert loaded == profile


def test_entry_for_matches_exact_cell_only() -> None:
    profile = load_token_usage_profile()
    found = profile.entry_for(
        step="find-mistakes",
        runtime="codex",
        model="uncalibrated-default",
        effort="medium",
    )
    assert found is not None
    assert found.step == "find-mistakes"
    assert found.runtime == "codex"
    missing = profile.entry_for(
        step="find-mistakes",
        runtime="codex",
        model="some-other-model",
        effort="medium",
    )
    assert missing is None


def test_entry_rejects_p90_below_p50() -> None:
    with pytest.raises(ValidationError):
        _entry(p50_total_tokens_per_message=200.0, p90_total_tokens_per_message=100.0)


def test_entry_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        _entry(benchmark_summary="never stored")


def test_entry_rejects_non_kebab_identifiers() -> None:
    with pytest.raises(ValidationError):
        _entry(runtime="Claude Code")
    with pytest.raises(ValidationError):
        _entry(step="find_mistakes")


def test_record_total_tokens_sums_all_categories() -> None:
    record = _record(fresh_input_tokens=2000, cached_input_tokens=300, output_tokens=200)
    assert record.total_tokens == 2500


def test_record_partition_key_covers_spec_fields() -> None:
    record = _record()
    assert record.partition_key() == (
        "claude-code",
        "pinned-model-id",
        "medium",
        "find-mistakes",
        1,
        1,
        1,
        25,
    )


def test_records_with_different_prompt_version_are_incompatible() -> None:
    base = _record()
    same_partition = _record(
        words=9000, utterances=50, recorded_at=datetime(2026, 2, 2, tzinfo=UTC)
    )
    other_prompt = _record(prompt_version=2)
    assert base.is_compatible_with(same_partition)
    assert not base.is_compatible_with(other_prompt)


def test_record_round_trips_through_jsonl(tmp_path: Path) -> None:
    records = [_record(), _record(prompt_version=2, words=500)]
    target = tmp_path / "local-history.jsonl"
    assert write_jsonl_models(target, records) == 2
    loaded = list(read_jsonl_models(target, CalibrationRecord))
    assert loaded == records


def test_record_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        _record(fresh_input_tokens=-1)
    with pytest.raises(ValidationError):
        _record(batch_size=0)
