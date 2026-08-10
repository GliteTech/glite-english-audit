"""CLI: remember what the user chose, before a run exists.

Run: ``uv run python -m glite_english_audit.pipeline.save_choice --period last-30-days``

Discovery ends with the user having answered two questions — which apps, and
how far back — but a run does not exist yet, so there was nowhere to put the
answer. It lived in the conversation and vanished with the terminal, which is
also what tempted the agent to tell the user it had been "recorded" when
nothing had.

This writes the answer beside the pending inventory so it survives.
``pipeline.start_run`` adopts it automatically, so the choice is made once and
reused rather than restated on the command line.

What is stored is only what the user said: application names, opaque labels
they excluded, and a period preset. No paths, no counts, no text. Consent is
deliberately not stored here — a remembered answer is a convenience, and
consent is not something to remember on someone's behalf.

A processing profile was stored too, until the question that produced it was
removed: it offered two model choices the run cannot make, because steps c, d
and e inherit the session's model.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.io import ensure_private_dir, read_model, write_model
from glite_english_audit.discovery.pending_expiry import PENDING_INVENTORY_MAX_AGE_DAYS
from glite_english_audit.paths import pending_inventory_dir

CHOICE_NAME = "pending-choice.json"


class PendingChoice(BaseModel):
    """What the user chose during setup, before a run existed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_preset: str
    include_sources: list[str] = Field(default_factory=list)
    exclude_sources: list[str] = Field(default_factory=list)
    exclude_labels: list[str] = Field(default_factory=list)
    chosen_at: datetime

    @field_validator("chosen_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "chosen_at must be timezone-aware"
            raise ValueError(msg)
        return value


def choice_path(*, inventory_dir: Path | None = None) -> Path:
    """Where the remembered choice lives, beside the inventory it refers to."""
    base = inventory_dir if inventory_dir is not None else pending_inventory_dir()
    return base / CHOICE_NAME


def save_choice(
    *,
    period_preset: str,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    inventory_dir: Path | None = None,
    now: datetime | None = None,
) -> PendingChoice:
    """Write the user's answer beside the pending inventory."""
    choice = PendingChoice(
        period_preset=period_preset,
        include_sources=include_sources or [],
        exclude_sources=exclude_sources or [],
        exclude_labels=exclude_labels or [],
        chosen_at=now if now is not None else utc_now(),
    )
    target = choice_path(inventory_dir=inventory_dir)
    ensure_private_dir(target.parent)
    write_model(target, choice)
    return choice


def load_choice(
    *, inventory_dir: Path | None = None, now: datetime | None = None
) -> PendingChoice | None:
    """The remembered choice, or None when there is no usable one.

    Absent, unreadable and too old are all the same answer, for the same
    reason: asking one question again costs a question, while acting on a stale
    answer costs an audit of the wrong sources.

    The age rule is the seven days the discovery skill already promised and
    nothing implemented. It matters more than it looks. ``start_run`` adopts
    this choice field by field whenever the caller passed nothing for that
    field, and the caller passes nothing for exclusions in the commonest case
    of all -- the user kept every default app. A choice left by some earlier
    setup conversation therefore re-applied its exclusions silently, and
    ``estimate`` never reads this file, so the preflight could price five apps
    while the run audited four.
    """
    target = choice_path(inventory_dir=inventory_dir)
    if not target.is_file():
        return None
    try:
        choice = read_model(target, PendingChoice)
    except ValueError:
        return None
    moment = now if now is not None else utc_now()
    if moment - choice.chosen_at > timedelta(days=PENDING_INVENTORY_MAX_AGE_DAYS):
        return None
    return choice


def clear_choice(*, inventory_dir: Path | None = None) -> bool:
    """Forget the remembered choice. True when one was there."""
    target = choice_path(inventory_dir=inventory_dir)
    if target.is_file():
        target.unlink()
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Remember the user's setup choice")
    parser.add_argument("--period", required=True)
    parser.add_argument("--include-source", action="append", default=None, metavar="APP")
    parser.add_argument("--exclude-source", action="append", default=None, metavar="APP")
    parser.add_argument("--exclude-label", action="append", default=None, metavar="LABEL")
    parser.add_argument("--inventory-dir", type=Path, default=None, help="test override")
    parser.add_argument("--clear", action="store_true", help="forget the remembered choice")
    arguments = parser.parse_args(argv)

    if arguments.clear:
        cleared = clear_choice(inventory_dir=arguments.inventory_dir)
        sys.stdout.write(json.dumps({"cleared": cleared}, indent=2) + "\n")
        return 0

    choice = save_choice(
        period_preset=arguments.period,
        include_sources=arguments.include_source,
        exclude_sources=arguments.exclude_source,
        exclude_labels=arguments.exclude_label,
        inventory_dir=arguments.inventory_dir,
    )
    sys.stdout.write(
        json.dumps(
            {
                "saved_to": str(choice_path(inventory_dir=arguments.inventory_dir)),
                "period": choice.period_preset,
                "include_sources": choice.include_sources,
                "exclude_sources": choice.exclude_sources,
                "exclude_labels": choice.exclude_labels,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
