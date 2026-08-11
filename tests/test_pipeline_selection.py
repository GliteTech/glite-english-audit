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

import pytest

from glite_english_audit.artifacts.enums import (
    Accessibility,
    AgentRuntime,
    OsEnvironment,
    Stability,
)
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.estimation.profile import load_token_usage_profile, resolve_models
from glite_english_audit.pipeline.start_run import resolve_selection, start_run
from glite_english_audit.runtime_session import (
    SESSION_EFFORT_KEY,
    SESSION_MODEL_KEY,
    UNKNOWN_SESSION_VALUE,
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


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
        created_at=_NOW,
    )


def test_default_reads_claude_code_and_nothing_else() -> None:
    """An audit reads one source, and the learner is not asked which.

    The question that used to be here -- which of these applications should I
    read? -- asked them to weigh privacy against volume with no way to judge
    either, and every app after the first adds setup and explanation while
    adding nothing a report needs.
    """
    selected = resolve_selection(_inventory(), runtime=AgentRuntime.CLAUDE_CODE)
    assert selected == ["claude_code-Claude-Code-1", "claude_code-Claude-Code-4"]


def test_a_machine_without_the_runtimes_own_history_selects_nothing() -> None:
    """The opposite of what this test used to assert, and deliberately.

    It used to read every other eligible source when Claude Code held nothing,
    on the reasoning that falling back beats refusing. That was defensible while
    the runtime was always Claude Code. It stopped being defensible the moment
    the runtime could be Codex: the same rule would take the learner's Claude
    Code writing and hand it to a different provider, while the skill told them
    it was reading their Codex history.

    Selecting nothing lets `start_run` refuse by name, and the run skill already
    owns the better answer -- it offers the other sources when the primary is
    too thin to be worth a report.
    """
    inventory = _inventory()
    without = inventory.model_copy(
        update={"records": [r for r in inventory.records if r.adapter_id != "claude_code"]}
    )
    assert resolve_selection(without, runtime=AgentRuntime.CLAUDE_CODE) == []


def test_another_app_is_one_flag_away() -> None:
    """The other adapters stay implemented, and stay reachable."""
    selected = resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, include_sources=["Codex"]
    )
    assert "codex-Codex-1" in selected


def test_excluding_the_only_default_source_selects_nothing() -> None:
    """Exclusion subtracts; it does not silently substitute another app.

    Dropping Claude Code leaves an empty selection, and `start_run` refuses an
    empty selection by name. Quietly auditing Codex instead would audit a source
    the learner never chose and the preflight never priced.
    """
    assert (
        resolve_selection(
            _inventory(), runtime=AgentRuntime.CLAUDE_CODE, exclude_sources=["Claude Code"]
        )
        == []
    )


def test_excluding_an_app_that_was_not_selected_changes_nothing() -> None:
    assert resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, exclude_sources=["codex"]
    ) == [
        "claude_code-Claude-Code-1",
        "claude_code-Claude-Code-4",
    ]


def test_exclude_one_project_by_its_opaque_label() -> None:
    selected = resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, exclude_labels=["Claude Code 4"]
    )
    assert selected == ["claude_code-Claude-Code-1"]


def test_include_a_beta_app_the_user_asked_for() -> None:
    selected = resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, include_sources=["Cursor"]
    )
    assert "cursor-Cursor-1" in selected


def test_labels_and_names_are_case_insensitive() -> None:
    assert resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, exclude_labels=["claude code 4"]
    ) == [
        "claude_code-Claude-Code-1",
    ]
    assert resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, exclude_sources=["CURSOR"]
    ) == resolve_selection(_inventory(), runtime=AgentRuntime.CLAUDE_CODE)


def test_an_empty_app_is_never_added_by_including_it() -> None:
    # Wispr Flow is present but holds nothing; asking for it adds no instance
    # rather than a run with nothing to read.
    assert "wispr_flow-Wispr-Flow-1" not in resolve_selection(
        _inventory(), runtime=AgentRuntime.CLAUDE_CODE, include_sources=["Wispr Flow"]
    )


def test_the_choice_reaches_the_manifest(tmp_path: Path) -> None:
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
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


def _observing(monkeypatch: pytest.MonkeyPatch, *, model: str | None, effort: str | None) -> None:
    """Make detection report a session, through the real chain start_run uses."""
    from glite_english_audit import runtime_session

    monkeypatch.setattr(runtime_session, "detect_model", lambda **_: model)
    monkeypatch.setattr(runtime_session, "detect_effort", lambda **_: effort)


def test_the_manifest_records_the_model_the_session_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed, never resolved from the calibration profile.

    The profile assumes claude-fable-5 for two of the three semantic steps, and
    the manifest recorded that. A real run had all 75 of its records read by
    claude-opus-5 — the per-file agents inherit the session's model and nothing
    pins one — so the manifest named a model that had read nothing, and a
    resume comparing it would have reused another model's judgments.
    """
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    _observing(monkeypatch, model="claude-opus-5", effort="xhigh")

    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        now=_NOW,
    )

    assert manifest.fingerprint.model_ids == {
        SESSION_MODEL_KEY: "claude-opus-5",
        SESSION_EFFORT_KEY: "xhigh",
    }
    resolved = resolve_models(
        load_token_usage_profile(), runtime="claude-code", processing_profile="recommended"
    )
    assert "claude-fable-5" in set(resolved.values()), "the profile still assumes another model"
    assert "claude-fable-5" not in set(manifest.fingerprint.model_ids.values())
    # Nor keyed by step, which is how a per-step resolution reached this field
    # and how it would read as three choices rather than one inheritance.
    assert set(resolved).isdisjoint(manifest.fingerprint.model_ids)


def test_a_session_it_cannot_read_is_recorded_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex sessions and any host without a readable transcript land here. The
    # honest record is that nobody knows, not the profile's model wearing the
    # authority of a manifest.
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    _observing(monkeypatch, model=None, effort=None)

    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        now=_NOW,
    )

    assert manifest.fingerprint.model_ids == {
        SESSION_MODEL_KEY: UNKNOWN_SESSION_VALUE,
        SESSION_EFFORT_KEY: UNKNOWN_SESSION_VALUE,
    }


def test_consent_is_absent_unless_the_caller_states_it(tmp_path: Path) -> None:
    # A consent timestamp is evidence that someone was asked and agreed.
    # Creating a run must never invent one.
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert manifest.consent.local_scan_confirmed_at is None
    assert manifest.consent.provider_transfer_confirmed_at is None


def test_each_consent_is_recorded_only_when_given(tmp_path: Path) -> None:
    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=tmp_path / "runs",
        inventory_dir=inventory_dir,
        local_scan_consent=True,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    # Agreeing to a local scan says nothing about sending text to a provider,
    # which specification 2.2 requires to be asked separately on every audit.
    assert manifest.consent.local_scan_confirmed_at is not None
    assert manifest.consent.provider_transfer_confirmed_at is None


def test_collect_refuses_to_read_source_data_without_local_scan_consent(tmp_path: Path) -> None:
    from glite_english_audit.pipeline.collect import collect

    inventory_dir = ensure_private_dir(tmp_path / "inv")
    write_model(inventory_dir / "source-inventory.json", _inventory())
    runs_root = tmp_path / "runs"
    manifest = start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        runs_root=runs_root,
        inventory_dir=inventory_dir,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="local-scan consent"):
        collect(manifest.run_id, runs_root=runs_root)


def test_under_codex_the_default_is_codex_and_nothing_else() -> None:
    """The whole point of the runtime being a parameter.

    Same inventory, same code path, different runtime: the audit reads the
    history of the agent it is running inside. That is what keeps the privacy
    argument true -- the messages were typed into the agent now reading them
    back, so reading them back discloses them to nobody new.
    """
    assert resolve_selection(_inventory(), runtime=AgentRuntime.CODEX) == ["codex-Codex-1"]


def test_under_codex_claude_code_is_not_read_by_default() -> None:
    """The failure this refactor exists to prevent, stated directly."""
    selected = resolve_selection(_inventory(), runtime=AgentRuntime.CODEX)
    assert not any(key.startswith("claude_code") for key in selected)


def test_under_codex_claude_code_is_still_one_flag_away() -> None:
    """Refusing the silent fallback is not refusing the explicit request."""
    selected = resolve_selection(
        _inventory(), runtime=AgentRuntime.CODEX, include_sources=["Claude Code"]
    )
    assert "claude_code-Claude-Code-1" in selected


def test_every_runtime_maps_to_an_adapter_that_exists() -> None:
    """A runtime added without an adapter would select nothing, silently."""
    from glite_english_audit.artifacts.models import PUBLIC_SOURCE_TYPES
    from glite_english_audit.pipeline.start_run import PRIMARY_ADAPTERS

    assert set(PRIMARY_ADAPTERS) == set(AgentRuntime)
    for adapter_id in PRIMARY_ADAPTERS.values():
        assert adapter_id in PUBLIC_SOURCE_TYPES, adapter_id
