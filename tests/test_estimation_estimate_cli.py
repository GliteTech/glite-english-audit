"""The preset estimate the period question is supposed to rest on.

Specification 2.4 says the agent computes and shows an estimate for every
preset before asking which period to audit. Nothing implemented that, so a
real run could only offer "many hours" — the exact hand-wave the requirement
exists to prevent. These tests pin the parts that make the table trustworthy:
the same selection rules the run will use, an interpolation that is honest
about being interpolation, a confidence label that does not present an
uncalibrated cell as measured, and output that carries no label or path.
"""

import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import (
    Accessibility,
    AgentRuntime,
    OsEnvironment,
    Stability,
)
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.english import and_list
from glite_english_audit.estimation.estimate import (
    EstimateReport,
    PresetRow,
    RuntimeSteps,
    SessionModel,
    build_notes,
    build_report,
    describe_session,
    distinct_rows,
    select_runtime_steps,
    step_units,
    window_counts,
    window_fraction,
)
from glite_english_audit.estimation.estimator import (
    ASSUMED_MESSAGES_PER_SESSION,
    EstimateConfidence,
    TimeRange,
    TokenEstimate,
)
from glite_english_audit.estimation.profile import load_token_usage_profile
from glite_english_audit.pipeline.start_run import PERIOD_PRESETS


def _preset_row(preset: str, words: int) -> PresetRow:
    """A row carrying only what recommend_preset reads."""
    return PresetRow(
        preset=preset,
        label=preset,
        words=words,
        utterances=max(1, words // 60),
        tokens=TokenEstimate(p50_tokens=0, p90_tokens=0),
        minutes=TimeRange(low_minutes=0.0, high_minutes=0.0),
        confidence=EstimateConfidence.LOW,
    )


_REPO = Path(__file__).resolve().parent.parent
_NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _record(
    adapter: str,
    label: str,
    *,
    messages: int = 1000,
    words_per_message: int = 30,
    first: datetime | None = _NOW - timedelta(days=100),
    last: datetime | None = _NOW,
    stability: Stability = Stability.STABLE,
) -> SourceInstanceRecord:
    key = f"{adapter}-{label}".replace(" ", "-")
    return SourceInstanceRecord(
        adapter_id=adapter,
        adapter_version="1.0.0",
        instance_key=key,
        opaque_label=label,
        storage_format="jsonl",
        schema_fingerprint="v2",
        path_hash="b" * 64,
        os_environment=OsEnvironment.MACOS,
        stability=stability,
        accessibility=Accessibility.FOUND,
        estimated_records=messages,
        earliest_timestamp=first,
        latest_timestamp=last,
        candidate_messages=messages,
        candidate_words=messages * words_per_message,
        candidate_bytes=messages * words_per_message * 6,
    )


def _write_inventory(directory: Path, records: list[SourceInstanceRecord]) -> Path:
    ensure_private_dir(directory)
    write_model(
        directory / "source-inventory.json",
        PrivateInventory(
            records=records,
            instance_paths={
                r.instance_key: f"/Users/someone/secret-project/{r.instance_key}" for r in records
            },
        ),
    )
    return directory


def _report(
    tmp_path: Path,
    records: list[SourceInstanceRecord],
    *,
    include_sources: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    concurrent_batches: int = 1,
) -> EstimateReport:
    directory = _write_inventory(tmp_path / "inventory", records)
    return build_report(
        inventory_dir=directory,
        runtime=AgentRuntime.CLAUDE_CODE,
        include_sources=include_sources,
        exclude_labels=exclude_labels,
        concurrent_batches=concurrent_batches,
        now=_NOW,
    )


def test_window_fraction_is_the_share_of_the_instance_span() -> None:
    record = _record("claude_code", "Claude Code 1")
    fraction = window_fraction(record, start=_NOW - timedelta(days=10), end=_NOW)
    assert fraction == pytest.approx(0.1)


def test_a_window_before_the_instance_existed_takes_nothing() -> None:
    record = _record("claude_code", "Claude Code 1")
    fraction = window_fraction(
        record, start=_NOW - timedelta(days=400), end=_NOW - timedelta(days=200)
    )
    assert fraction == 0.0


def test_an_instant_instance_counts_in_full_inside_the_window() -> None:
    moment = _NOW - timedelta(days=3)
    record = _record("claude_code", "Claude Code 1", first=moment, last=moment)
    assert window_fraction(record, start=_NOW - timedelta(days=7), end=_NOW) == 1.0
    assert window_fraction(record, start=_NOW - timedelta(days=1), end=_NOW) == 0.0


def test_an_undated_instance_is_counted_in_full_and_reported() -> None:
    # It cannot be placed in time, so the honest move is to count it and say so
    # rather than to drop it silently or pretend it fits a short window.
    record = _record("claude_code", "Claude Code 1", first=None, last=None)
    counts = window_counts([record], start=_NOW - timedelta(days=1), end=_NOW)
    assert counts.utterances == 1000
    assert counts.undated_instances == 1


def test_step_units_cover_the_three_steps_that_exist() -> None:
    """The estimate priced two steps the pipeline does not run.

    verify-findings was deleted and create-safe-records was merged into
    find-mistakes, but the estimator still charged 388 and 175 units per 1,000
    messages for them — 563 of 2,533 units, 22% of a number the user consents
    to at the preflight, spent on work nobody does. The confidentiality check,
    which does run, was priced at nothing.
    """
    units = step_units(1000)
    assert units.judge_authorship == 1000
    assert units.find_mistakes == 970
    # One agent per session file, so the unit here is a session, not a record.
    # Derived from the constant: recalibrating the pooled sessions-per-message
    # figure should move this with it, not break it.
    assert units.confirm_confidentiality == math.ceil(1000 / ASSUMED_MESSAGES_PER_SESSION)
    assert units.total == 1000 + 970 + math.ceil(1000 / ASSUMED_MESSAGES_PER_SESSION)


def test_every_preset_gets_a_row_in_preset_order(tmp_path: Path) -> None:
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert [row.preset for row in report.presets] == list(PERIOD_PRESETS)


def test_words_never_shrink_as_the_window_grows(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        [
            _record("claude_code", "Claude Code 1"),
            _record("codex", "Codex 1", messages=400, first=_NOW - timedelta(days=20)),
        ],
    )
    words = [row.words for row in report.presets]
    assert words == sorted(words)


def test_everything_matches_the_discovered_totals(tmp_path: Path) -> None:
    """ "Everything" means every message of the sources the run will read.

    Which is Claude Code alone by default, so the Codex record here is present
    to prove it is *not* counted -- the estimate must describe the run that will
    happen, not the machine.
    """
    claude = _record("claude_code", "Claude Code 1")
    records = [
        claude,
        _record("codex", "Codex 1", messages=400, first=_NOW - timedelta(days=20)),
    ]
    report = _report(tmp_path, records)
    everything = next(row for row in report.presets if row.preset == "everything")
    assert everything.words == claude.candidate_words
    assert everything.utterances == claude.candidate_messages


def test_a_short_window_takes_its_share_of_a_long_history(tmp_path: Path) -> None:
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    week = next(row for row in report.presets if row.preset == "last-7-days")
    assert week.words == pytest.approx(30_000 * 0.07, rel=0.01)


def test_the_selection_rules_are_the_ones_start_run_will_apply(tmp_path: Path) -> None:
    records = [
        _record("claude_code", "Claude Code 1"),
        _record("cursor", "Cursor 1", messages=5000, stability=Stability.BETA),
    ]
    default = _report(tmp_path, records)
    with_cursor = _report(tmp_path, records, include_sources=["Cursor"])
    assert default.selected_instances == 1
    assert with_cursor.selected_instances == 2
    assert with_cursor.presets[-1].words > default.presets[-1].words


def test_dropping_an_instance_by_its_label_shrinks_the_estimate(tmp_path: Path) -> None:
    records = [
        _record("claude_code", "Claude Code 1"),
        _record("claude_code", "Claude Code 4", messages=200),
    ]
    full = _report(tmp_path, records)
    trimmed = _report(tmp_path, records, exclude_labels=["Claude Code 4"])
    assert trimmed.presets[-1].utterances < full.presets[-1].utterances


def test_an_empty_selection_is_an_error_not_a_table_of_zeros(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to estimate"):
        _report(tmp_path, [_record("claude_code", "Claude Code 1", messages=0)])


def test_a_partly_calibrated_runtime_says_the_run_can_exceed_the_range(tmp_path: Path) -> None:
    # claude-code has 10 measured find-mistakes samples but only 7 authorship
    # and 1 confidentiality sample, under the 10-sample minimum of 13.7. How
    # many samples back which cell is the maintainer's business; what the user
    # gets from it is that the ranges can be exceeded.
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert all(row.confidence is EstimateConfidence.LOW for row in report.presets)
    assert any("can take longer and use more" in note for note in report.notes)


def test_an_uncalibrated_runtime_says_so_instead_of_showing_a_bare_number(
    tmp_path: Path,
) -> None:
    # codex has no measurement at all. The table must not present its numbers
    # as if someone had timed them.
    directory = _write_inventory(tmp_path / "inventory", [_record("codex", "Codex 1")])
    report = build_report(inventory_dir=directory, runtime=AgentRuntime.CODEX, now=_NOW)
    assert all(row.confidence is EstimateConfidence.LOW for row in report.presets)
    assert "Estimates, not measurements" in report.table
    assert "can take longer and use more" in report.table


def test_the_table_says_money_is_the_part_it_cannot_tell_you(tmp_path: Path) -> None:
    """Price is unreadable; the subscription allowance is not.

    The note used to say "quota and price are unavailable", and the quota half
    was false: ``glite_english_audit.subscription`` reads the host's cached
    utilization and reset time, and the run skill shows them at the preflight.
    Announcing a missing percentage here would also contradict that skill,
    which rules the absence of headroom not worth a sentence.
    """
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert "No price is available" in report.table
    assert "quota" not in report.table.lower()
    assert "subscription" not in report.table.lower()


def test_the_notes_stay_few_enough_to_be_read(tmp_path: Path) -> None:
    """Nine caveats under a table are a wall, and a wall is skimmed.

    Callers are told to relay every note, so each one added spends the
    attention of the rest. Three survive here, four when a source cannot be
    placed in time.
    """
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert len(report.notes) == 3


def test_no_note_says_a_word_only_this_repository_uses(tmp_path: Path) -> None:
    """The notes reach a learner, not a maintainer.

    Step identifiers, model IDs, an effort level, cached input, and batches are
    this project's vocabulary. Each was in a note, and none of them told the
    reader anything they could act on.
    """
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    internal = (
        "judge-authorship",
        "find-mistakes",
        "confirm-confidentiality",
        "claude-fable-5",
        "claude-opus-5",
        "effort",
        "cached input",
        "batch",
        "p90",
        "confidence",
    )
    for note in report.notes:
        for word in internal:
            assert word not in note.lower(), f"{word!r} in {note!r}"


def test_duration_tracks_units_not_token_volume(tmp_path: Path) -> None:
    # The 2026-08-08 profile is dominated by cached input, so a token-throughput
    # duration would report hundreds of hours for a month of writing. A month of
    # 1,000 messages is a working day at worst, not a working month.
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    month = next(row for row in report.presets if row.preset == "last-30-days")
    assert 0 < month.minutes.low_minutes < month.minutes.high_minutes
    assert month.minutes.high_minutes < 12 * 60


def test_parallel_batches_divide_the_estimated_time(tmp_path: Path) -> None:
    one = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    four = _report(tmp_path, [_record("claude_code", "Claude Code 1")], concurrent_batches=4)
    assert four.presets[-1].minutes.high_minutes == pytest.approx(
        one.presets[-1].minutes.high_minutes / 4
    )


def test_the_command_prints_no_label_and_no_path(tmp_path: Path) -> None:
    directory = _write_inventory(
        tmp_path / "inventory",
        [_record("claude_code", "Claude Code 1", first=datetime.now(tz=UTC) - timedelta(days=60))],
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "glite_english_audit.estimation.estimate",
            "--inventory-dir",
            str(directory),
        ],
        cwd=_REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert len(payload["presets"]) == len(PERIOD_PRESETS)
    assert (
        payload["presets"][0]["tokens"]["p90_tokens"]
        >= payload["presets"][0]["tokens"]["p50_tokens"]
    )
    assert "Claude Code 1" not in result.stdout
    assert "secret-project" not in result.stdout
    assert "/Users/" not in result.stdout


def test_the_command_explains_a_missing_inventory_instead_of_a_traceback(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "glite_english_audit.estimation.estimate",
            "--inventory-dir",
            str(tmp_path / "absent"),
        ],
        cwd=_REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "discovery.inventory" in result.stderr


def test_a_window_that_matches_everything_is_printed_once(tmp_path: Path) -> None:
    """The duplicate row is dropped rather than explained.

    A history 100 days long makes Last year and Everything the same run, and
    the table used to print both and add a caveat saying they were identical on
    purpose — a note whose only job was to defend the row above it. Everything
    is the row kept: it is the only one counted rather than interpolated, and
    the narrower label promises a limit that does not happen.
    """
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert "Last year" not in report.table
    assert "Everything" in report.table
    assert "identical on purpose" not in report.table
    # Every preset still carries its numbers for a caller that was handed one.
    assert [row.preset for row in report.presets] == list(PERIOD_PRESETS)


def test_only_windows_that_differ_survive_the_collapse(tmp_path: Path) -> None:
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    kept = [row.preset for row in distinct_rows(report.presets)]
    everything = next(row for row in report.presets if row.preset == "everything")
    assert "everything" in kept
    for row in report.presets:
        same = row.words == everything.words and row.utterances == everything.utterances
        assert (row.preset in kept) is (row.preset == "everything" or not same)


def test_lists_inside_a_note_are_joined_as_english_not_as_a_column() -> None:
    """A comma-joined list reads as data. Inside a sentence it needs "and"."""
    assert and_list([]) == ""
    assert and_list(["A"]) == "A"
    assert and_list(["A", "B"]) == "A and B"
    assert and_list(["A", "B", "C"]) == "A, B, and C"


def test_the_undated_source_note_agrees_in_number() -> None:
    """The count decides the noun, the verb, and the pronoun after it."""
    steps = select_runtime_steps(load_token_usage_profile(), runtime="claude-code")
    session = describe_session(steps)
    one = build_notes(steps=steps, session=session, undated_instances=1)
    assert any("1 source reports no dates" in note and "it counts in full" in note for note in one)
    assert not any("1 sources" in note for note in one)

    several = build_notes(steps=steps, session=session, undated_instances=3)
    assert any(
        "3 sources report no dates" in note and "they count in full" in note for note in several
    )


def _steps_measured_on(model: str, effort: str) -> RuntimeSteps:
    """A three-cell selection whose every cell was measured on one model.

    The committed profile no longer has that property — the confidentiality
    cell was measured on a different model from the other two — so a test about
    a session that matches everything has to build the case it is about.
    """
    measured = select_runtime_steps(load_token_usage_profile(), runtime="claude-code")
    cell = measured.find_mistakes.model_copy(update={"model": model, "effort": effort})
    return RuntimeSteps(judge_authorship=cell, find_mistakes=cell, confirm_confidentiality=cell)


def test_a_session_that_matches_what_was_measured_is_not_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown or matching session is not evidence of a mismatch.

    The mismatch is one of the two reasons the "can run higher" note prints, so
    treating every session as mismatched would pin that note on permanently,
    and a caveat that is always there stops being read.
    """
    from glite_english_audit.estimation import estimate as module

    monkeypatch.setattr(module, "detect_model", lambda: "claude-fable-5")
    monkeypatch.setattr(module, "detect_effort", lambda: "medium")
    matching = describe_session(_steps_measured_on("claude-fable-5", "medium"))
    assert matching.measured_elsewhere is False
    assert matching.model == "claude-fable-5"

    monkeypatch.setattr(module, "detect_model", lambda: None)
    monkeypatch.setattr(module, "detect_effort", lambda: None)
    steps = select_runtime_steps(load_token_usage_profile(), runtime="claude-code")
    unknown = describe_session(steps)
    assert unknown.measured_elsewhere is False
    # Unknown is reported as unknown. Filling it in from the cells it is being
    # compared against is the substitution this whole change removes.
    assert unknown.model is None
    assert unknown.effort is None
    assert unknown.measured_models == ("claude-fable-5", "claude-opus-5")


def test_matching_one_cell_out_of_three_is_not_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confidentiality cell was measured on a different model from the rest.

    A membership test — is the running model anywhere in the profile? — would
    call an opus session calibrated while two of its three steps were still
    described by measurements of another model.

    What the user is told is that the numbers can run high, not which model
    measured which step: they cannot choose a model at the period question, and
    a model ID is this repository's vocabulary rather than theirs.
    """
    from glite_english_audit.estimation import estimate as module

    monkeypatch.setattr(module, "detect_model", lambda: "claude-opus-5")
    monkeypatch.setattr(module, "detect_effort", lambda: "xhigh")
    steps = select_runtime_steps(load_token_usage_profile(), runtime="claude-code")
    assert "claude-opus-5" in {entry.model for entry in steps.entries()}
    session = describe_session(steps)
    assert session.measured_elsewhere is True
    notes = build_notes(steps=steps, session=session, undated_instances=0)
    assert any("can take longer and use more" in note for note in notes)
    assert not any("claude-opus-5" in note for note in notes)


def test_the_report_hands_the_preflight_the_running_model_and_the_measured_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight has to state one and disclaim the other, so it gets both.

    It used to be handed neither, and said the calibration profile's model —
    one screen before the user agreed to let a model read everything they had
    written, and with nothing in the product able to make it true.
    """
    from glite_english_audit.estimation import estimate as module

    monkeypatch.setattr(module, "detect_model", lambda: "claude-opus-5")
    monkeypatch.setattr(module, "detect_effort", lambda: "xhigh")
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])

    assert report.session.model == "claude-opus-5"
    assert report.session.effort == "xhigh"
    assert "claude-fable-5" in report.session.measured_models
    assert report.session.measured_elsewhere is True


def test_a_model_that_cannot_be_read_is_null_rather_than_the_measured_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Null is what the preflight turns into "I cannot tell you which model".
    # Any substitute here becomes a sentence the product cannot keep.
    from glite_english_audit.estimation import estimate as module

    monkeypatch.setattr(module, "detect_model", lambda: None)
    monkeypatch.setattr(module, "detect_effort", lambda: None)
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])

    assert report.session.model is None
    assert report.session.effort is None
    assert report.session.measured_models
    payload = json.loads(report.model_dump_json())
    assert payload["session"]["model"] is None


def test_every_session_field_the_preflight_quotes_exists() -> None:
    """The preflight tells an agent to read `session.model` and its neighbours.

    Prose drifts from code silently, and this project has shipped that defect
    three times. The model line is the one sentence in a run that may not be
    improvised, so every name it quotes is checked against the model that
    produces it — a stale name there sends an agent back to inventing one.
    """
    skill = (_REPO / "skills/run-english-audit/SKILL.md").read_text(encoding="utf-8")
    quoted = set(re.findall(r"`session\.([a-z_]+)`", skill))
    assert quoted, "the preflight must say where the model it states comes from"
    assert quoted <= set(SessionModel.model_fields)
    assert "session" in EstimateReport.model_fields


def test_a_matching_model_with_a_wrong_effort_still_counts_as_measured_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Effort is a profile key too, so the right model at an unmeasured effort
    # is still an estimate of a different run.
    from glite_english_audit.estimation import estimate as module

    monkeypatch.setattr(module, "detect_model", lambda: "claude-fable-5")
    monkeypatch.setattr(module, "detect_effort", lambda: "xhigh")
    assert describe_session(_steps_measured_on("claude-fable-5", "medium")).measured_elsewhere


def test_the_recommendation_is_sized_in_words(tmp_path: Path) -> None:
    """Words are what discovery counts, so words are what the target is in.

    The size was worked out once from a measured finding rate. That arithmetic
    does not run again per learner: the rate came from one person's writing and
    error rates differ enormously between people, so re-applying it would present
    a single sample as a prediction about someone nobody has measured.
    """
    from glite_english_audit.estimation import estimate as module

    assert not hasattr(module, "expected_findings"), (
        "a per-learner findings prediction came back; the product says words"
    )
    assert isinstance(module.TARGET_WORDS, int)


def test_the_window_nearest_the_target_wins_not_the_first_one_past_it(
    tmp_path: Path,
) -> None:
    """Overshooting costs the learner an afternoon for repeated writing.

    Measured on a real machine: two weeks held 47,000 words and thirty days
    110,000. A rule that took the shortest window past a threshold would have
    doubled the run to overshoot the target.
    """
    from glite_english_audit.estimation.estimate import TARGET_WORDS, recommend_preset

    rows = [
        _preset_row("last-7-days", TARGET_WORDS // 2),
        _preset_row("last-14-days", TARGET_WORDS - 5_000),
        _preset_row("last-30-days", TARGET_WORDS * 2),
    ]
    assert recommend_preset(rows) == "last-14-days"


def test_the_target_is_sized_by_the_report_not_by_appetite() -> None:
    """20,000 words, derived, not chosen: the report is designed for 200-300
    verified mistakes and the production corpus measures 13.1 per 1,000 words.
    The previous value, 60,000, expected ~790 -- the size class the report
    chain measurably failed at before the input was capped."""
    from glite_english_audit.estimation.estimate import TARGET_WORDS

    assert TARGET_WORDS == 20_000


def test_a_thin_machine_is_offered_everything_it_has(tmp_path: Path) -> None:
    from glite_english_audit.estimation.estimate import recommend_preset

    rows = [_preset_row("last-7-days", 200), _preset_row("everything", 3_000)]
    assert recommend_preset(rows) == "everything"
