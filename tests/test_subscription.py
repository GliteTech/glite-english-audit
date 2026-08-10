"""Reading the host's cached allowance, and refusing to guess when it is absent.

The preflight told every user "quota and price are unavailable". Half of that was
false: the host caches the percentage used and the reset time, and withholds only
the money. Before a run the product says may take two hours, headroom is the fact
that decides.

The file being read belongs to Claude Code, not to this project, so most of these
tests are about the shapes it might take tomorrow. A run must not fail over a
number it only wanted for a sentence.
"""

import json
from pathlib import Path

from glite_english_audit.subscription import (
    STALE_AFTER_SECONDS,
    UNKNOWN,
    read_allowance,
)

_NOW = 1_786_300_000.0


def _write(home: Path, payload: object) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
    return home


def _config(**windows: object) -> dict[str, object]:
    return {
        "cachedUsageUtilization": {
            "fetchedAtMs": _NOW * 1000,
            "utilization": windows,
        }
    }


def test_the_windows_a_subscriber_recognises_are_reported(tmp_path: Path) -> None:
    home = _write(
        tmp_path,
        _config(
            five_hour={"utilization": 17, "resets_at": "2026-08-09T20:00:00Z"},
            seven_day={"utilization": 1, "resets_at": "2026-08-15T23:59:59Z"},
        ),
    )
    allowance = read_allowance(home=home, now=_NOW)
    assert allowance.known
    assert [(w.name, w.utilization) for w in allowance.windows] == [
        ("five_hour", 17),
        ("seven_day", 1),
    ]


def test_the_tightest_window_is_the_one_that_will_bite(tmp_path: Path) -> None:
    # A user at 3% of the week and 90% of the hour is about to be throttled, and
    # the number worth showing is the 90.
    home = _write(
        tmp_path,
        _config(
            five_hour={"utilization": 90, "resets_at": "2026-08-09T20:00:00Z"},
            seven_day={"utilization": 3, "resets_at": None},
        ),
    )
    tightest = read_allowance(home=home, now=_NOW).tightest
    assert tightest is not None
    assert tightest.name == "five_hour"


def test_the_hosts_own_codenames_are_not_shown_to_a_user(tmp_path: Path) -> None:
    """The real file carries buckets named nimbus_quill, tangelo, iguana_necktie.

    A percentage the user cannot place is worse than no percentage: they cannot
    tell whether it constrains them, and they cannot look it up anywhere.
    """
    home = _write(
        tmp_path,
        _config(
            seven_day={"utilization": 1, "resets_at": None},
            nimbus_quill={"utilization": 40, "resets_at": None},
            tangelo={"utilization": 99, "resets_at": None},
        ),
    )
    assert [w.name for w in read_allowance(home=home, now=_NOW).windows] == ["seven_day"]


def test_extra_usage_is_the_overage_setting_and_not_a_window(tmp_path: Path) -> None:
    # It carries a `utilization` key like the windows do, so a naive read counts
    # the paid-overage meter as an allowance the user is spending.
    home = _write(
        tmp_path,
        _config(
            seven_day={"utilization": 1, "resets_at": None},
            extra_usage={"is_enabled": False, "utilization": 0, "monthly_limit": 2000},
        ),
    )
    allowance = read_allowance(home=home, now=_NOW)
    assert [w.name for w in allowance.windows] == ["seven_day"]
    assert allowance.overage_enabled is False


def test_a_window_the_host_left_null_is_not_reported_as_zero(tmp_path: Path) -> None:
    # Null means the host tracks no such window for this account, which is most
    # of them. Reporting them as 0% claims headroom nobody measured.
    home = _write(tmp_path, _config(five_hour=None, seven_day={"utilization": 4}))
    assert [w.name for w in read_allowance(home=home, now=_NOW).windows] == ["seven_day"]


def test_a_cached_figure_says_how_old_it_is(tmp_path: Path) -> None:
    """Nothing here refreshes the cache, so the age travels with the number.

    A percentage shown without its age implies a live check that never happened.
    """
    home = _write(tmp_path, _config(seven_day={"utilization": 1}))
    fresh = read_allowance(home=home, now=_NOW + 60)
    assert fresh.age_seconds is not None
    assert not fresh.stale

    old = read_allowance(home=home, now=_NOW + STALE_AFTER_SECONDS + 1)
    assert old.stale


def test_every_shape_the_host_might_change_to_reads_as_unknown(tmp_path: Path) -> None:
    """The file is another product's private config, not an interface.

    Each of these is a plausible next release. None may raise, because a run must
    not fail over a number it only wanted for a sentence.
    """
    payloads: tuple[object, ...] = (
        {},
        {"cachedUsageUtilization": None},
        {"cachedUsageUtilization": {"utilization": None}},
        {"cachedUsageUtilization": {"utilization": {"seven_day": "lots"}}},
        {"cachedUsageUtilization": {"utilization": {"seven_day": {"utilization": None}}}},
        {"cachedUsageUtilization": []},
        [],
        "not an object at all",
    )
    for payload in payloads:
        home = _write(tmp_path / str(abs(hash(str(payload)))), payload)
        assert read_allowance(home=home, now=_NOW).known is False


def test_a_machine_without_the_file_is_an_ordinary_answer(tmp_path: Path) -> None:
    # A Codex session has no such file. That is not an error state.
    assert read_allowance(home=tmp_path / "nowhere", now=_NOW) == UNKNOWN


def test_unreadable_json_is_unknown_rather_than_a_crash(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    assert read_allowance(home=tmp_path, now=_NOW).known is False


def test_a_true_boolean_is_not_mistaken_for_a_percentage(tmp_path: Path) -> None:
    # bool is an int in Python, so `"utilization": true` would read as 1% used.
    home = _write(tmp_path, _config(seven_day={"utilization": True}))
    assert read_allowance(home=home, now=_NOW).known is False


def test_nothing_read_here_carries_an_account_identifier(tmp_path: Path) -> None:
    """The cache also holds an accountUuid, which nothing needs and nothing takes.

    This module exists to put a percentage in a sentence, so the percentage, its
    window and its reset are the whole of what leaves it.
    """
    home = _write(
        tmp_path, _config(seven_day={"utilization": 1, "resets_at": "2026-08-15T00:00:00Z"})
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "cachedUsageUtilization": {
                    "fetchedAtMs": _NOW * 1000,
                    "accountUuid": "e73dc35e-b309-4f52-89df-9c425ce98933",
                    "utilization": {"seven_day": {"utilization": 1}},
                }
            }
        ),
        encoding="utf-8",
    )
    allowance = read_allowance(home=home, now=_NOW)
    assert "e73dc35e" not in repr(allowance)
