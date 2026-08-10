"""The launcher's first question, answered by a command instead of by improvisation.

``list_unfinished`` and ``describe_resume`` were written, tested and called by
nothing. The agent facing step 2 therefore built the answer itself, and a real
session opened with two ``python -c`` snippets, a ``sed`` through a source file,
an ``ls`` of a package and sixty lines of a test file before it could say "no
unfinished audit" -- all of it visible to someone who had asked one question.

This file pins the command that replaced that: what it offers, what it sweeps,
what it leaves alone, and what it must never print.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import AgentRuntime, OsEnvironment, RunStatus
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, ConsentState
from glite_english_audit.pipeline.resume_check import (
    build_report,
    live_adapter_versions,
    main,
    remove_empty_run_dirs,
)
from glite_english_audit.state.machine import advance_run
from glite_english_audit.state.run_store import create_run, save_manifest

_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def _fingerprint() -> CompatibilityFingerprint:
    return CompatibilityFingerprint.model_validate(
        {
            "adapter_versions": {"codex": "1.0.0"},
            "artifact_schema_version": 1,
            "tokenizer_version": "1.0.0",
            "skill_versions": {"find-english-mistakes": 1},
            "prompt_versions": {},
            "model_ids": {},
            "consent_policy_version": "1",
        }
    )


def _processing_run(root: Path) -> str:
    manifest = create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1"),
        _fingerprint(),
        root=root,
    )
    for target in (RunStatus.SELECTING, RunStatus.AWAITING_PREFLIGHT, RunStatus.PROCESSING):
        manifest.status = advance_run(manifest.status, target)
    save_manifest(manifest, root=root)
    return manifest.run_id


def test_a_directory_with_no_files_is_removed_rather_than_explained(tmp_path: Path) -> None:
    """A start that failed before writing a manifest leaves one of these.

    It cannot be resumed and cannot be expired -- there is no timestamped
    private file to date it from -- so it accumulates until the launcher
    explains "an empty directory from an aborted start" to somebody who asked
    whether they had an unfinished audit.
    """
    debris = tmp_path / ("run-" + "a" * 32)
    (debris / "steps" / "a-collected").mkdir(parents=True)

    assert remove_empty_run_dirs(tmp_path) == [debris.name]
    assert not debris.exists()


def test_a_directory_holding_any_file_is_left_for_retention(tmp_path: Path) -> None:
    """Anything holding a file is data until retention says otherwise.

    Deleting on sight would race the thirty-day rule and win.
    """
    run = tmp_path / ("run-" + "b" * 32)
    (run / "steps").mkdir(parents=True)
    (run / "steps" / "session-0001.jsonl").write_text("{}\n", encoding="utf-8")

    assert remove_empty_run_dirs(tmp_path) == []
    assert (run / "steps" / "session-0001.jsonl").is_file()


def test_an_adapter_that_no_longer_exists_does_not_read_as_a_version_change() -> None:
    """Resume asks whether *this* run's sources changed under it.

    A removed adapter keeps its recorded version, so the comparison reports no
    change and the removal surfaces at collection, where it actually bites,
    rather than as a fingerprint mismatch nobody can act on.
    """
    assert live_adapter_versions({"no_such_adapter": "9.9.9"}) == {"no_such_adapter": "9.9.9"}


def test_a_real_adapter_reports_the_version_installed_today() -> None:
    live = live_adapter_versions({"codex": "0.0.0-recorded"})
    assert live["codex"] != "0.0.0-recorded"


def test_an_interrupted_run_is_offered_with_a_reason(tmp_path: Path) -> None:
    run_id = _processing_run(tmp_path)

    report = build_report(root=tmp_path, now=_NOW)
    runs = report["unfinished"]

    assert isinstance(runs, list)
    assert [run["run_id"] for run in runs] == [run_id]
    assert runs[0]["detail"], "every offer carries the policy's own wording"
    assert report["offerable"] == 1


def test_nothing_in_the_report_is_a_path_or_a_filename(tmp_path: Path) -> None:
    """This runs before any consent to send, so it says numbers and ids only."""
    _processing_run(tmp_path)

    printed = json.dumps(build_report(root=tmp_path, now=_NOW))

    assert str(tmp_path) not in printed
    assert ".jsonl" not in printed
    assert "/Users/" not in printed


def test_the_cli_prints_parsable_json_on_an_empty_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--runs-root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["unfinished"] == []
    assert report["offerable"] == 0
    assert report["removed_empty"] == []
