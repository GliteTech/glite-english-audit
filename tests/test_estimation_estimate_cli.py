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
from glite_english_audit.estimation.estimate import (
    CUSTOM_ROW_LABEL,
    EstimateReport,
    build_report,
    step_units,
    window_counts,
    window_fraction,
)
from glite_english_audit.estimation.estimator import EstimateConfidence
from glite_english_audit.pipeline.start_run import PERIOD_PRESETS

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


def test_step_units_shrink_downstream() -> None:
    units = step_units(1000)
    assert units.find_mistakes == 1000
    assert units.verify_findings == 400
    assert units.create_safe_records == 180
    assert units.total == 1580


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
    records = [
        _record("claude_code", "Claude Code 1"),
        _record("codex", "Codex 1", messages=400, first=_NOW - timedelta(days=20)),
    ]
    report = _report(tmp_path, records)
    everything = next(row for row in report.presets if row.preset == "everything")
    assert everything.words == sum(r.candidate_words for r in records)
    assert everything.utterances == sum(r.candidate_messages for r in records)


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


def test_a_partly_calibrated_runtime_is_low_confidence(tmp_path: Path) -> None:
    # claude-code has 10 measured find-mistakes batches but only 4 verify and 1
    # safe-record batches, which is under the 10-batch minimum of 13.7.
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert all(row.confidence is EstimateConfidence.LOW for row in report.presets)
    assert any("measured batches" in note for note in report.notes)


def test_an_uncalibrated_runtime_says_so_instead_of_showing_a_bare_number(
    tmp_path: Path,
) -> None:
    directory = _write_inventory(tmp_path / "inventory", [_record("codex", "Codex 1")])
    report = build_report(inventory_dir=directory, runtime=AgentRuntime.CODEX, now=_NOW)
    assert all(row.confidence is EstimateConfidence.LOW for row in report.presets)
    assert any("Never measured" in note for note in report.notes)
    assert "low confidence" in report.table


def test_the_table_states_that_quota_and_price_are_unavailable(tmp_path: Path) -> None:
    report = _report(tmp_path, [_record("claude_code", "Claude Code 1")])
    assert "Quota and price are unavailable" in report.table
    assert CUSTOM_ROW_LABEL in report.table
    assert "Calculated after dates are entered" in report.table


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
