"""Content-free append-only run event log (specification, 6.5).

The log records artifact IDs, hashes, diagnostic codes, and lifecycle events
for debugging and resumption. It is structured and privacy-minimized by
construction: the record model has no field that could carry source text, and
messages are limited to registered diagnostic codes plus enum-like event
names.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.envelope import utc_now

EventKind = Literal[
    "artifact_produced",
    "artifact_verified",
    "artifact_promoted",
    "artifact_replaced",
    "downstream_invalidated",
    "item_quarantined",
    "checkpoint_written",
    "run_status_changed",
    "snapshot_created",
    "snapshot_removed",
    "cleanup_completed",
]


class RunEvent(BaseModel):
    """One content-free event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    kind: EventKind
    stage_id: StageId | None = None
    artifact_id: str | None = None
    artifact_hash: str | None = None
    replaced_artifact_id: str | None = None
    diagnostic_codes: list[str] = []
    detail_code: str | None = None

    @field_validator("at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "event timestamps must be timezone-aware"
            raise ValueError(msg)
        return value


def event_log_path(run_dir: Path) -> Path:
    """The event log location inside one run directory."""
    return run_dir / "logs" / "events.jsonl"


def append_event(run_dir: Path, event: RunEvent) -> None:
    """Append one event. Plain append is durable enough for a debug log."""
    path = event_log_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")


def log_event(
    run_dir: Path,
    kind: EventKind,
    *,
    stage_id: StageId | None = None,
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
    replaced_artifact_id: str | None = None,
    diagnostic_codes: list[str] | None = None,
    detail_code: str | None = None,
) -> RunEvent:
    """Build, append, and return one event stamped with the current time."""
    event = RunEvent(
        at=utc_now(),
        kind=kind,
        stage_id=stage_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        replaced_artifact_id=replaced_artifact_id,
        diagnostic_codes=diagnostic_codes if diagnostic_codes is not None else [],
        detail_code=detail_code,
    )
    append_event(run_dir, event)
    return event


def read_events(run_dir: Path) -> list[RunEvent]:
    """Read every event in order. Missing log means no events."""
    path = event_log_path(run_dir)
    if not path.is_file():
        return []
    events: list[RunEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            events.append(RunEvent.model_validate_json(stripped))
    return events
