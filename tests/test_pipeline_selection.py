"""The user's spoken choice must reach the manifest.

The agent never sees an instance key: discovery shows it opaque labels and
application names, and the keys stay in the private inventory. So the choice
arrives in those terms and is resolved locally (specification, 2.4). Before
this, selection accepted only instance keys, which meant "drop Cursor" or
"drop Claude Code 4" could be agreed in conversation and then silently fail to
reach the run.
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
from glite_english_audit.pipeline.start_run import resolve_selection, start_run


def _record(
    adapter: str,
    label: str,
    *,
    stability: Stability = Stability.STABLE,
    messages: int = 10,
) -> SourceInstanceRecord:
    key = f"{adapter}-{label}".replace(" ", "-")
    return SourceInstanceRecord(
        adapter_id=adapter,
        adapter_version="1.0.0",
        instance_key=key,
        opaque_label=label,
        storage_format="jsonl",
        schema_fingerprint="v2",
        path_hash="a" * 64,
        os_environment=OsEnvironment.MACOS,
        stability=stability,
        accessibility=Accessibility.FOUND,
        estimated_records=messages,
        candidate_messages=messages,
        candidate_words=messages * 10,
        candidate_bytes=messages * 50,
    )


def _inventory() -> PrivateInventory:
    records = [
        _record("claude_code", "Claude Code 1"),
        _record("claude_code", "Claude Code 4"),
        _record("codex", "Codex 1"),
        _record("cursor", "Cursor 1", stability=Stability.BETA),
        _record("wispr_flow", "Wispr Flow 1", messages=0),
    ]
    return PrivateInventory(
        records=records,
        instance_paths={r.instance_key: f"/somewhere/{r.instance_key}" for r in records},
    )


def test_default_takes_stable_sources_with_content() -> None:
    selected = resolve_selection(_inventory())
    assert selected == ["claude_code-Claude-Code-1", "claude_code-Claude-Code-4", "codex-Codex-1"]


def test_exclude_a_whole_app_by_the_name_the_user_saw() -> None:
    selected = resolve_selection(_inventory(), exclude_sources=["Claude Code"])
    assert selected == ["codex-Codex-1"]


def test_exclude_a_whole_app_by_its_public_id() -> None:
    assert resolve_selection(_inventory(), exclude_sources=["codex"]) == [
        "claude_code-Claude-Code-1",
        "claude_code-Claude-Code-4",
    ]


def test_exclude_one_instance_by_its_opaque_label() -> None:
    selected = resolve_selection(_inventory(), exclude_labels=["Claude Code 4"])
    assert selected == ["claude_code-Claude-Code-1", "codex-Codex-1"]


def test_include_a_beta_app_the_user_asked_for() -> None:
    selected = resolve_selection(_inventory(), include_sources=["Cursor"])
    assert "cursor-Cursor-1" in selected


def test_labels_and_names_are_case_insensitive() -> None:
    assert resolve_selection(_inventory(), exclude_labels=["claude code 4"]) == [
        "claude_code-Claude-Code-1",
        "codex-Codex-1",
    ]
    assert resolve_selection(_inventory(), exclude_sources=["CURSOR"]) == resolve_selection(
        _inventory()
    )


def test_an_empty_app_is_never_added_by_including_it() -> None:
    # Wispr Flow is present but holds nothing; asking for it adds no instance
    # rather than a run with nothing to read.
    assert "wispr_flow-Wispr-Flow-1" not in resolve_selection(
        _inventory(), include_sources=["Wispr Flow"]
    )


def test_the_choice_reaches_the_manifest(tmp_path: Path) -> None:
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        exclude_labels=["Claude Code 4"],
        include_sources=["Cursor"],
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert manifest.selection is not None
    chosen = set(manifest.selection.selected_instance_keys)
    assert "claude_code-Claude-Code-4" not in chosen
    assert "cursor-Cursor-1" in chosen
    assert "claude_code-Claude-Code-4" in set(manifest.selection.excluded_instance_keys)
