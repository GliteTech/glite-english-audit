"""How much of the subscription allowance is left, when the host will say.

The preflight told every user "quota and price are unavailable", which was half
wrong and wrong in the more useful half. Claude Code caches the account's
utilization in ``~/.claude.json``, and the provider supplies the percentage used
and the moment it resets while withholding the money:

    "five_hour":  {"utilization": 0,  "resets_at": null,  "limit_dollars": null}
    "seven_day":  {"utilization": 1,  "resets_at": "...", "limit_dollars": null}

So headroom is knowable and price is not. Before a run the product says may take
two hours, headroom is the fact that decides, and the run was refusing to look
it up.

Three limits on what this may claim, all of them shaping the code below.

**It is a cache, not a reading.** ``fetchedAtMs`` says when the host last
refreshed it, and nothing here triggers a refresh. A number presented without
its age would imply a live check that never happened, so the age travels with
the number and the caller is expected to show it.

**It belongs to another product.** ``~/.claude.json`` is Claude Code's private
file, not an interface anyone promised to keep. Every field is read defensively
and any surprise degrades to "unknown" rather than raising, because a run must
not fail over a number it only wanted for a sentence.

**It is Claude Code only.** A Codex session has no equivalent, so absence is an
ordinary answer here rather than an error.
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".claude.json"

# The host tracks buckets under names that mean nothing outside it — codenames
# for experiments, plus `extra_usage`, which is the paid-overage setting rather
# than an allowance window. Only the two windows a subscriber can recognise are
# reported, with their per-model variants. An unrecognised name is skipped
# rather than shown, because a percentage the user cannot place is worse than
# no percentage.
_WINDOW_NAMES = re.compile(r"^(five_hour|seven_day)(_[a-z_]+)?$")

# Beyond this the cached figure describes a different afternoon. Reported rather
# than suppressed: a user who has been running for hours is exactly the one who
# wants to know their last reading is old.
STALE_AFTER_SECONDS = 60 * 60


@dataclass(frozen=True)
class Window:
    """One allowance window: how much is used, and when it resets."""

    name: str
    utilization: int
    resets_at: str | None


@dataclass(frozen=True)
class Allowance:
    """What the host will say about the account's remaining headroom.

    ``windows`` is empty when nothing could be read, which is the honest answer
    on a Codex session or a machine whose host has never cached a figure.
    """

    windows: tuple[Window, ...]
    overage_enabled: bool | None
    age_seconds: float | None

    @property
    def known(self) -> bool:
        """Whether anything was read at all."""
        return bool(self.windows)

    @property
    def stale(self) -> bool:
        """Whether the cached figure is old enough to say so."""
        return self.age_seconds is not None and self.age_seconds > STALE_AFTER_SECONDS

    @property
    def tightest(self) -> Window | None:
        """The window closest to its limit, which is the one that will bite."""
        return max(self.windows, key=lambda w: w.utilization, default=None)


UNKNOWN = Allowance(windows=(), overage_enabled=None, age_seconds=None)


def read_allowance(*, home: Path | None = None, now: float | None = None) -> Allowance:
    """Read the host's cached utilization, or return :data:`UNKNOWN`.

    Never raises. Every failure — no file, unreadable file, unparsable JSON, a
    shape that changed under us — is the same answer as a Codex session: the
    host did not say.
    """
    path = (home if home is not None else Path.home()) / CONFIG_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNKNOWN
    if not isinstance(payload, dict):
        return UNKNOWN
    cached = payload.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return UNKNOWN

    windows: list[Window] = []
    utilization = cached.get("utilization")
    if isinstance(utilization, dict):
        for name, value in utilization.items():
            if not _WINDOW_NAMES.fullmatch(name):
                continue
            window = _window(name, value)
            if window is not None:
                windows.append(window)

    return Allowance(
        windows=tuple(sorted(windows, key=lambda w: w.name)),
        overage_enabled=_overage(cached),
        age_seconds=_age(cached, now),
    )


def _window(name: str, value: object) -> Window | None:
    """One utilization entry, or ``None`` when the host left it empty.

    A null entry means the host tracks no such window for this account, which is
    most of them. Reporting those as zero would claim headroom nobody measured.
    """
    if not isinstance(value, dict):
        return None
    used = value.get("utilization")
    if not isinstance(used, int | float) or isinstance(used, bool):
        return None
    resets_at = value.get("resets_at")
    return Window(
        name=name,
        utilization=int(used),
        resets_at=resets_at if isinstance(resets_at, str) else None,
    )


def _overage(cached: dict[str, object]) -> bool | None:
    """Whether paid overage is switched on, when the host says.

    The preflight promises not to enable it. Reading the setting turns that
    promise into an observation, which is the stronger of the two.
    """
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return None
    extra = utilization.get("extra_usage")
    if not isinstance(extra, dict):
        return None
    enabled = extra.get("is_enabled")
    return enabled if isinstance(enabled, bool) else None


def _age(cached: dict[str, object], now: float | None) -> float | None:
    """Seconds since the host refreshed the figure, when it timestamped it."""
    fetched = cached.get("fetchedAtMs")
    if not isinstance(fetched, int | float) or isinstance(fetched, bool):
        return None
    moment = now if now is not None else time.time()
    return max(0.0, moment - fetched / 1000.0)
