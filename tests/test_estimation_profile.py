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
    resolve_models,
)
from glite_english_audit.paths import repo_root

# The steps the pipeline actually runs. verify-findings was deleted and
# create-safe-records was merged into find-mistakes, so neither may be priced.
EXPECTED_STEPS = {
    "judge-authorship",
    "find-mistakes",
    "confirm-confidentiality",
}
RETIRED_STEPS = {"verify-findings", "create-safe-records"}
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
    # Claude Code cells were measured on real owner data: authorship and
    # mistakes on claude-fable-5 (2026-08-08), confidentiality on claude-opus-5
    # (2026-08-09). Codex cells stay uncalibrated until a run under Codex.
    profile = load_token_usage_profile()
    for entry in profile.entries:
        if entry.runtime == "claude-code":
            assert entry.model in {"claude-fable-5", "claude-opus-5"}
            assert entry.messages_measured > 0
            assert not entry.is_uncalibrated
            assert entry.p90_total_tokens_per_message >= entry.p50_total_tokens_per_message
        else:
            assert entry.model == "uncalibrated-default"
            assert entry.messages_measured == 0
            assert entry.is_uncalibrated
    assert [e for e in profile.low_confidence_entries() if e.runtime == "codex"]


def test_a_retired_step_is_kept_as_a_record_and_priced_by_nothing() -> None:
    """Both halves matter, and they pull in opposite directions.

    verify-findings and create-safe-records were really measured, so deleting
    the numbers would destroy a record of work that happened. But the pipeline
    stopped running either step, and while they sat in ``entries`` they were
    22% of the token total the user is asked to consent to. Keeping them out of
    the priced set is what makes keeping them safe.
    """
    profile = load_token_usage_profile()
    priced = {entry.step for entry in profile.entries}
    retired = {entry.step for entry in profile.retired_entries}
    assert priced == EXPECTED_STEPS
    assert retired == RETIRED_STEPS
    assert priced.isdisjoint(retired)
    assert all(entry.messages_measured >= 0 for entry in profile.retired_entries)
    # Every model resolution and every estimate reads ``entries`` alone.
    resolved = resolve_models(profile, runtime="claude-code", processing_profile="recommended")
    assert RETIRED_STEPS.isdisjoint(resolved)


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


def test_judge_authorship_cell_reproduces_the_run_it_was_measured_from() -> None:
    """The step-c cell must still predict the run that produced it.

    On 2026-08-09 a real audit judged authorship for 198 candidate utterances
    in eight concurrent batches and consumed 4,418,856 tokens end to end
    (803,449 fresh input, 3,578,485 cached input, 36,922 output). A profile
    edit that drifts far from that is either a new measurement, which should
    replace these numbers, or a mistake.

    The band is deliberately wide. Eight batches is a small sample and the
    coefficients are per-message medians, so exact agreement would be a
    coincidence rather than a check.
    """
    measured_tokens = 4_418_856
    measured_utterances = 198

    entry = load_token_usage_profile().entry_for(
        step="judge-authorship", runtime="claude-code", model="claude-fable-5", effort="medium"
    )
    assert entry is not None
    predicted = entry.p50_total_tokens_per_message * measured_utterances
    assert 0.8 <= predicted / measured_tokens <= 1.2, (
        f"the committed cell predicts {predicted:,.0f} tokens for the run that measured "
        f"{measured_tokens:,}"
    )
    assert entry.messages_measured == measured_utterances


def test_confirm_confidentiality_cell_reproduces_the_run_it_was_measured_from() -> None:
    """Step e used to be priced at nothing. This pins what replaced the zero.

    On 2026-08-09 a real audit ran the confidentiality check over 31 session
    files and consumed 3,242,189 tokens (798,083 fresh input, 2,417,478 cached
    input, 26,628 output). The cell charges a fixed cost per session file plus a
    per-session total, because the run showed the record count predicting
    nothing: the 15 sessions holding no record cost as much as the 13 that did.

    One run on one machine, so the band is wide and the cell stays
    low-confidence. A profile edit that leaves this band is either a new
    measurement, which should say so, or a mistake.
    """
    measured_tokens = 3_242_189
    measured_sessions = 31

    entry = load_token_usage_profile().entry_for(
        step="confirm-confidentiality",
        runtime="claude-code",
        model="claude-opus-5",
        effort="xhigh",
    )
    assert entry is not None
    predicted = measured_sessions * (
        entry.p50_total_tokens_per_message + entry.fixed_input_tokens_per_batch
    )
    assert 0.8 <= predicted / measured_tokens <= 1.2, (
        f"the committed cell predicts {predicted:,.0f} tokens for the run that measured "
        f"{measured_tokens:,}"
    )
    assert entry.messages_measured == measured_sessions
    # One run is one sample under the 13.7 rule, not 31 of them.
    assert entry.messages_measured // 25 < 10


def test_a_profile_resolves_one_priced_cell_per_step() -> None:
    """What the function is for: which measurement each step is priced from.

    Not which model will run. Steps c, d and e inherit the session's model, so
    a profile can only say what was measured — the test below holds it to that.
    """
    profile = load_token_usage_profile()
    resolved = resolve_models(profile, runtime="claude-code", processing_profile="recommended")
    assert set(resolved) == EXPECTED_STEPS
    assert all(model for model in resolved.values())


def test_an_unknown_processing_profile_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown processing profile"):
        resolve_models(
            load_token_usage_profile(), runtime="claude-code", processing_profile="economical"
        )


def test_nothing_presents_a_resolved_model_as_what_will_run() -> None:
    """The defect this whole change exists to end.

    A resolved model reached the run manifest as ``model_ids`` and the
    preflight as "runs on Claude Fable 5", one screen before the user agreed to
    let a model read everything they had written. A real run then did every one
    of its 75 records on claude-opus-5, because the per-file agents inherit the
    session's model and this product pins nothing.

    So: the manifest records the session, the skill never names a profile
    model, and the setup no longer offers a choice between two of them.
    """
    manifest_source = (repo_root() / "src/glite_english_audit/pipeline/start_run.py").read_text(
        encoding="utf-8"
    )
    assert "observed_model_ids()" in manifest_source
    assert "resolve_models" not in manifest_source

    skill = (repo_root() / "skills/run-english-audit/SKILL.md").read_text(encoding="utf-8")
    for model in {
        entry.model
        for entry in load_token_usage_profile().entries
        if entry.runtime == "claude-code"
    }:
        # Neither the identifier nor its product name: "Claude Fable 5" is how
        # the preflight said it, and that sentence is the one being deleted.
        assert model not in skill
        assert model.replace("-", " ").title() not in skill
    assert "profiles_differ" not in skill
    assert "Maximum assurance" not in skill
