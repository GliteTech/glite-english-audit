"""A map of where the user's data lives must not outlive its purpose.

Discovery runs before any run exists, so it leaves its result in a pending
location. On the owner's own machine that file held sixty absolute paths under
their home directory. Inside a run such a map expires with the run under the
30-day rule; the pending copy had no owner and no expiry, so abandoning the
setup conversation left it there permanently.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import Accessibility, OsEnvironment, Stability
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.discovery.pending_expiry import (
    PENDING_INVENTORY_MAX_AGE_DAYS,
    expire_pending_inventory,
    is_stale,
    pending_inventory_path,
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _inventory(created_at: datetime | None) -> PrivateInventory:
    record = SourceInstanceRecord(
        adapter_id="claude_code",
        adapter_version="1.0.0",
        instance_key="claude-code-1",
        opaque_label="Claude Code 1",
        storage_format="jsonl",
        schema_fingerprint="v2",
        path_hash="a" * 64,
        os_environment=OsEnvironment.MACOS,
        stability=Stability.STABLE,
        accessibility=Accessibility.FOUND,
        estimated_records=10,
        candidate_messages=10,
        candidate_words=100,
        candidate_bytes=500,
    )
    return PrivateInventory(
        records=[record],
        instance_paths={"claude-code-1": "/Users/someone/.claude/projects"},
        created_at=created_at,
    )


def _write(directory: Path, created_at: datetime | None) -> Path:
    ensure_private_dir(directory)
    target = pending_inventory_path(inventory_dir=directory)
    write_model(target, _inventory(created_at))
    return target


def test_a_fresh_inventory_survives(tmp_path: Path) -> None:
    target = _write(tmp_path, _NOW - timedelta(days=1))
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is False
    assert target.is_file()


def test_an_inventory_past_the_window_is_deleted(tmp_path: Path) -> None:
    target = _write(tmp_path, _NOW - timedelta(days=PENDING_INVENTORY_MAX_AGE_DAYS + 1))
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is True
    assert not target.exists()


def test_the_boundary_day_is_not_yet_stale(tmp_path: Path) -> None:
    target = _write(tmp_path, _NOW - timedelta(days=PENDING_INVENTORY_MAX_AGE_DAYS))
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is False
    assert target.is_file()


def test_an_inventory_without_a_date_counts_as_stale(tmp_path: Path) -> None:
    # Written before expiry existed. The safe reading of an unknown age is
    # "too old": the alternative keeps private paths forever on the strength
    # of a missing field.
    target = _write(tmp_path, None)
    assert is_stale(_inventory(None), now=_NOW) is True
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is True
    assert not target.exists()


def test_an_unreadable_file_at_that_path_is_deleted(tmp_path: Path) -> None:
    # A file here that does not parse as an inventory is not something to keep
    # for inspection. It is a file of unknown content where private paths live.
    ensure_private_dir(tmp_path)
    target = pending_inventory_path(inventory_dir=tmp_path)
    target.write_text("{not json", encoding="utf-8")
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is True
    assert not target.exists()


def test_no_inventory_is_not_an_error(tmp_path: Path) -> None:
    assert expire_pending_inventory(inventory_dir=tmp_path, now=_NOW) is False


def test_a_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    # Comparing a naive datetime to an aware one raises, which would turn
    # expiry into a crash on the first inventory written by a tool that
    # dropped the zone.
    naive = _inventory(datetime(2026, 8, 8, 12, 0))
    assert is_stale(naive, now=_NOW) is False


def test_start_run_refuses_a_stale_inventory(tmp_path: Path) -> None:
    from glite_english_audit.artifacts.enums import AgentRuntime
    from glite_english_audit.pipeline.start_run import start_run

    inventory_dir = tmp_path / "inv"
    _write(inventory_dir, _NOW - timedelta(days=PENDING_INVENTORY_MAX_AGE_DAYS + 1))
    with pytest.raises(ValueError, match="run discovery again"):
        start_run(
            runtime=AgentRuntime.CLAUDE_CODE,
            os_environment_value="macos",
            preset="everything",
            instance_keys=None,
            runs_root=tmp_path / "runs",
            inventory_dir=inventory_dir,
            now=_NOW,
        )
