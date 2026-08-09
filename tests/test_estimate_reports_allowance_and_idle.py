"""The preflight's two missing facts, now carried by the command that prints them.

``subscription.read_allowance`` existed, was tested, was described in the skill,
and nothing called it. With no command behind it an agent must improvise one,
and an improvised read returns a raw dataclass: the percentage gets quoted and
``age_seconds`` gets dropped, which is the one field the module computes so that
a cache is never mistaken for a live check.

Separately, sources are chosen before the period, so the period can empty a
source that was picked. An app whose history ended months ago contributes
nothing to a seven-day window, and reading the selection back as both apps is
true of the boxes ticked and false about the run being approved.

Neither is fixable in prose. A fact the agent has to fetch for itself is a fact
it will fetch differently every time, so both now travel with the estimate.

Every value below is invented. Nothing here records what any real run found.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.artifacts.enums import (
    Accessibility,
    AgentRuntime,
    OsEnvironment,
    Stability,
)
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.estimation.estimate import (
    AllowanceReport,
    describe_age,
    describe_allowance,
    idle_sources,
    window_counts,
)
from glite_english_audit.paths import repo_root

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


def test_a_sibling_products_bucket_never_becomes_your_allowance(tmp_path: Path) -> None:
    """The window names were a prefix match, and real hosts write more than two.

    `seven_day_cowork` and `seven_day_oauth_apps` are other products' quotas, not
    per-model variants of this one, and `tightest` is a plain maximum -- so one
    stranger at 88% displaced this run's own bucket at 4% and the preflight
    called it "your allowance". A name pattern cannot tell a variant from a
    stranger, so it is no longer asked to.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "cachedUsageUtilization": {
            "fetchedAtMs": int(_NOW_SECONDS * 1000),
            "utilization": {
                "five_hour": {"utilization": 3},
                "seven_day": {"utilization": 4},
                "seven_day_cowork": {"utilization": 91},
                "seven_day_oauth_apps": {"utilization": 88},
                "seven_day_opus": {"utilization": 77},
            },
        }
    }
    (tmp_path / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    report = describe_allowance(home=tmp_path, now=_NOW_SECONDS)
    assert report.tightest_window == "seven_day"
    assert report.utilization == 4


def test_a_codex_run_is_told_nothing_about_a_claude_code_subscription(tmp_path: Path) -> None:
    """The file belongs to Claude Code, and the skill forbids naming another runtime.

    A developer running Codex on a machine that also has Claude Code installed
    was shown a subscription with nothing to do with the provider doing the work.
    """
    home = _home(tmp_path, _NOW_SECONDS)
    assert describe_allowance(home=home, now=_NOW_SECONDS).known is True
    for runtime in (AgentRuntime.CODEX,):
        report = describe_allowance(runtime=runtime, home=home, now=_NOW_SECONDS)
        assert report.known is False
        assert report.utilization is None
    assert (
        describe_allowance(runtime=AgentRuntime.CLAUDE_CODE, home=home, now=_NOW_SECONDS).known
        is True
    )


def test_a_reset_time_the_host_did_not_write_as_a_timestamp_is_dropped(tmp_path: Path) -> None:
    """`resets_at` is the only free text crossing into agent context.

    The estimate command promises aggregate numbers only, and this value comes
    from a file this project does not own.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "cachedUsageUtilization": {
            "fetchedAtMs": int(_NOW_SECONDS * 1000),
            "utilization": {
                "seven_day": {"utilization": 1, "resets_at": "/Users/someone/secret/path.txt"}
            },
        }
    }
    (tmp_path / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    report = describe_allowance(home=tmp_path, now=_NOW_SECONDS)
    assert report.known is True
    assert report.resets_at is None


def test_a_source_whose_share_rounds_to_zero_is_idle() -> None:
    """`fraction > 0` is not a contribution; the totals bill round(words * fraction).

    A lightly used instance spread over years rounds to zero words in a
    seven-day window while a raw-fraction test still calls it live -- the exact
    symptom idle_sources exists to catch, which survived its first version.
    """
    spread_thin = _record(
        "aider", "Aider 1", messages=4, first=_NOW - timedelta(days=1825), last=_NOW
    )
    start, end = _NOW - timedelta(days=7), _NOW
    counts = window_counts([spread_thin], start=start, end=end)
    assert counts.words == 0 and counts.utterances == 0
    assert idle_sources([spread_thin], start=start, end=end) == ("aider",)


def test_every_allowance_field_the_preflight_quotes_exists() -> None:
    """The same guard `session.*` already has, for the six names beside it."""
    skill = (repo_root() / "skills" / "run-english-audit" / "SKILL.md").read_text(encoding="utf-8")
    quoted = set(re.findall(r"`allowance\.([a-z_]+)`", skill))
    assert quoted, "the preflight must say where the allowance it states comes from"
    unknown = sorted(quoted - set(AllowanceReport.model_fields))
    assert not unknown, f"skill quotes allowance fields that do not exist: {unknown}"
