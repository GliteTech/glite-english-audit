"""A choice made during setup must survive the terminal that made it.

Discovery ends with the user having answered which apps and how far back, but
no run exists yet, so the answer had nowhere to live. It stayed in the
conversation and vanished with it — which is also what tempted the agent to
report it as "recorded" when nothing had been written.
"""

from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import (
    Accessibility,
    AgentRuntime,
    OsEnvironment,
    Stability,
)
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.pipeline.save_choice import (
    PendingChoice,
    choice_path,
    clear_choice,
    load_choice,
    save_choice,
)
from glite_english_audit.pipeline.start_run import start_run


def _record(adapter: str, label: str) -> SourceInstanceRecord:
    return SourceInstanceRecord(
        adapter_id=adapter,
        adapter_version="1.0.0",
        instance_key=f"{adapter}-{label}".replace(" ", "-"),
        opaque_label=label,
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


def _seed(tmp_path: Path) -> Path:
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    records = [
        _record("claude_code", "Claude Code 1"),
        _record("claude_code", "Claude Code 4"),
        _record("codex", "Codex 1"),
    ]
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(
            records=records,
            instance_paths={r.instance_key: f"/somewhere/{r.instance_key}" for r in records},
        ),
    )
    return inventory_dir


def test_round_trip(tmp_path: Path) -> None:
    inventory_dir = _seed(tmp_path)
    saved = save_choice(
        period_preset="last-7-days",
        exclude_sources=["Codex"],
        exclude_labels=["Claude Code 4"],
        inventory_dir=inventory_dir,
    )
    loaded = load_choice(inventory_dir=inventory_dir)
    assert loaded == saved
    assert loaded is not None
    assert loaded.period_preset == "last-7-days"


def test_absent_when_nothing_was_chosen(tmp_path: Path) -> None:
    assert load_choice(inventory_dir=_seed(tmp_path)) is None


def test_the_stored_choice_carries_no_paths_or_counts(tmp_path: Path) -> None:
    # It holds only what the user said, so a remembered answer is not a second
    # copy of the private inventory.
    inventory_dir = _seed(tmp_path)
    save_choice(
        period_preset="everything", exclude_labels=["Claude Code 4"], inventory_dir=inventory_dir
    )
    blob = choice_path(inventory_dir=inventory_dir).read_text(encoding="utf-8")
    assert "/somewhere" not in blob
    assert "instance_key" not in blob
    assert "candidate" not in blob
    assert set(PendingChoice.model_fields) == {
        "period_preset",
        "processing_profile",
        "include_sources",
        "exclude_sources",
        "exclude_labels",
        "chosen_at",
    }


def test_start_run_adopts_the_remembered_choice(tmp_path: Path) -> None:
    inventory_dir = _seed(tmp_path)
    save_choice(
        period_preset="last-7-days",
        processing_profile="maximum-assurance",
        exclude_labels=["Claude Code 4"],
        inventory_dir=inventory_dir,
    )
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset=None,
        instance_keys=None,
        processing_profile=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert manifest.selection is not None
    assert manifest.selection.period.preset == "last-7-days"
    assert manifest.selection.processing_profile == "maximum-assurance"
    assert "claude_code-Claude-Code-4" not in set(manifest.selection.selected_instance_keys)


def test_an_explicit_argument_beats_the_remembered_one(tmp_path: Path) -> None:
    inventory_dir = _seed(tmp_path)
    save_choice(period_preset="last-7-days", inventory_dir=inventory_dir)
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert manifest.selection is not None
    assert manifest.selection.period.preset == "everything"


def test_a_corrupt_choice_is_treated_as_absent(tmp_path: Path) -> None:
    # Asking again costs one question; guessing at a stale answer costs an
    # audit of the wrong sources.
    inventory_dir = _seed(tmp_path)
    save_choice(period_preset="everything", inventory_dir=inventory_dir)
    choice_path(inventory_dir=inventory_dir).write_text("{not json", encoding="utf-8")
    assert load_choice(inventory_dir=inventory_dir) is None


def test_clearing_forgets_it(tmp_path: Path) -> None:
    inventory_dir = _seed(tmp_path)
    save_choice(period_preset="everything", inventory_dir=inventory_dir)
    assert clear_choice(inventory_dir=inventory_dir) is True
    assert load_choice(inventory_dir=inventory_dir) is None
    assert clear_choice(inventory_dir=inventory_dir) is False
