"""The preflight's two missing facts, now carried by the command that prints them.

Both defects here were found in a real session rather than by the suite.

``subscription.read_allowance`` existed, was tested, was described in the skill,
and nothing called it. So the agent wrote its own one-liner, received a raw
dataclass and reported "a weekly allowance that is 1% used" from a reading six
hours old -- dropping the one field the module computes specifically so that a
cache is never mistaken for a live check.

Separately, sources are chosen before the period, so the period can empty a
source the user picked. A run of Codex and Cursor over the last 7 days is a
Codex run: Cursor's data stopped in June. The selection was read back as "Codex
and Cursor", which is true of the boxes ticked and false about the run being
approved.

Neither is fixable in prose. A fact the agent has to fetch for itself is a fact
it will fetch differently every time, so both now travel with the estimate.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.artifacts.enums import Accessibility, OsEnvironment, Stability
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.estimation.estimate import (
    AllowanceReport,
    describe_age,
    describe_allowance,
    idle_sources,
)

_NOW_SECONDS = 1_786_300_000.0
_NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _record(
    adapter: str,
    label: str,
    *,
    messages: int = 1000,
    first: datetime | None = _NOW - timedelta(days=100),
    last: datetime | None = _NOW,
) -> SourceInstanceRecord:
    return SourceInstanceRecord(
        adapter_id=adapter,
        adapter_version="1.0.0",
        instance_key=f"{adapter}-{label}".replace(" ", "-"),
        opaque_label=label,
        storage_format="jsonl",
        schema_fingerprint="v2",
        path_hash="b" * 64,
        os_environment=OsEnvironment.MACOS,
        stability=Stability.STABLE,
        accessibility=Accessibility.FOUND,
        estimated_records=messages,
        earliest_timestamp=first,
        latest_timestamp=last,
        candidate_messages=messages,
        candidate_words=messages * 30,
        candidate_bytes=messages * 180,
    )


def _home(tmp_path: Path, fetched_at: float) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "cachedUsageUtilization": {
            "fetchedAtMs": int(fetched_at * 1000),
            "utilization": {"seven_day": {"utilization": 1, "resets_at": "2026-08-15T23:59:59Z"}},
        }
    }
    (tmp_path / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_the_reading_carries_its_age_in_words() -> None:
    """The agent gets "6 hours ago", not 21960.08, because it must say one of them."""
    assert describe_age(None) is None
    assert describe_age(30) == "just now"
    assert describe_age(60) == "1 minute ago"
    assert describe_age(21_960) == "6 hours ago"
    assert describe_age(60 * 60 * 49) == "2 days ago"


def test_a_six_hour_old_reading_is_marked_stale(tmp_path: Path) -> None:
    """The exact reading from the session that prompted this file."""
    report = describe_allowance(home=_home(tmp_path, _NOW_SECONDS - 21_960), now=_NOW_SECONDS)
    assert report.known
    assert report.utilization == 1
    assert report.tightest_window == "seven_day"
    assert report.age_phrase == "6 hours ago"
    assert report.stale is True


def test_a_machine_that_never_cached_one_says_so_without_raising(tmp_path: Path) -> None:
    report = describe_allowance(home=tmp_path / "nowhere", now=_NOW_SECONDS)
    assert report.known is False
    assert report.utilization is None
    assert report.age_phrase is None
    assert report.stale is False


def test_no_field_invites_a_sum_that_cannot_be_done() -> None:
    """The host reports a percentage with no denominator.

    Any field claiming this run's share of the allowance would be invented, so
    there is none, and the skill states that limit in words instead.
    """
    forbidden = {"share", "remaining_tokens", "tokens_left", "fits"}
    assert not forbidden & set(AllowanceReport.model_fields)


def test_a_source_outside_the_window_is_named_as_idle() -> None:
    """Cursor stopped in June; the window is the last 7 days of August."""
    records = [
        _record("codex", "Codex 1", first=_NOW - timedelta(days=300), last=_NOW),
        _record(
            "cursor",
            "Cursor 1",
            first=datetime(2025, 11, 7, tzinfo=UTC),
            last=datetime(2026, 6, 5, tzinfo=UTC),
        ),
    ]
    assert idle_sources(records, start=_NOW - timedelta(days=7), end=_NOW) == ("cursor",)


def test_an_app_is_idle_only_when_every_instance_of_it_is() -> None:
    """Two Cursor workspaces, one still active: Cursor is not idle."""
    records = [
        _record(
            "cursor",
            "Cursor 1",
            first=datetime(2025, 11, 7, tzinfo=UTC),
            last=datetime(2026, 6, 5, tzinfo=UTC),
        ),
        _record("cursor", "Cursor 2", first=_NOW - timedelta(days=3), last=_NOW),
    ]
    assert idle_sources(records, start=_NOW - timedelta(days=7), end=_NOW) == ()


def test_an_undated_source_is_never_called_idle() -> None:
    """window_counts charges undated instances in full, and this must agree.

    Calling one idle while the totals bill it would put two numbers from the
    same command in contradiction.
    """
    records = [_record("aider", "Aider 1", first=None, last=None)]
    assert idle_sources(records, start=_NOW - timedelta(days=7), end=_NOW) == ()
