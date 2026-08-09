"""Tests for the content-free run event log."""

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.state.event_log import RunEvent, log_event, read_events


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    first = log_event(
        tmp_path,
        "artifact_produced",
        step_id=StepId.D_MISTAKES,
        artifact_id="art-" + "a" * 32,
        artifact_hash="b" * 64,
    )
    second = log_event(
        tmp_path,
        "artifact_replaced",
        step_id=StepId.D_MISTAKES,
        artifact_id="art-" + "c" * 32,
        replaced_artifact_id="art-" + "a" * 32,
        diagnostic_codes=["LINEAGE_HASH_MISMATCH"],
    )
    events = read_events(tmp_path)
    assert events == [first, second]


def test_read_events_empty_when_no_log(tmp_path: Path) -> None:
    assert read_events(tmp_path) == []


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        RunEvent(at=datetime(2026, 8, 8), kind="checkpoint_written")


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        log_event(tmp_path, "raw_text_logged")  # type: ignore[arg-type]


def test_model_has_no_free_text_field() -> None:
    # Privacy by construction: every field is an ID, hash, code, enum, or time.
    allowed = {
        "at",
        "kind",
        "step_id",
        "artifact_id",
        "artifact_hash",
        "replaced_artifact_id",
        "diagnostic_codes",
        "detail_code",
    }
    assert set(RunEvent.model_fields) == allowed
