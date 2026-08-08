"""Runtime root resolution per OS and repository-owned snapshot locations."""

import sys
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import OsEnvironment
from glite_english_audit.paths import (
    detect_os_environment,
    repo_root,
    run_dir,
    runs_root,
    runtime_root,
    snapshot_dir,
)


def test_runtime_root_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / "Library" / "Application Support" / "Glite English Audit"
    assert runtime_root(OsEnvironment.MACOS) == expected


def test_runtime_root_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    expected = tmp_path / "AppData" / "Local" / "Glite English Audit"
    assert runtime_root(OsEnvironment.WINDOWS) == expected


def test_runtime_root_windows_requires_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(RuntimeError):
        runtime_root(OsEnvironment.WINDOWS)


@pytest.mark.parametrize("environment", [OsEnvironment.LINUX, OsEnvironment.WSL])
def test_runtime_root_linux_prefers_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: OsEnvironment
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert runtime_root(environment) == tmp_path / "state" / "glite-english-audit"


@pytest.mark.parametrize("environment", [OsEnvironment.LINUX, OsEnvironment.WSL])
def test_runtime_root_linux_falls_back_to_home_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: OsEnvironment
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".local" / "state" / "glite-english-audit"
    assert runtime_root(environment) == expected


def test_runs_root_and_run_dir_nest_under_runtime_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = runtime_root(OsEnvironment.MACOS)
    assert runs_root(OsEnvironment.MACOS) == root / "runs"
    run_id = "run-" + "0" * 32
    assert run_dir(run_id, OsEnvironment.MACOS) == root / "runs" / run_id


@pytest.mark.skipif(sys.platform != "darwin", reason="detection test targets this macOS machine")
def test_detect_os_environment_returns_macos() -> None:
    assert detect_os_environment() is OsEnvironment.MACOS


def test_repo_root_is_the_repository_checkout() -> None:
    root = repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "glite_english_audit").is_dir()


def test_snapshot_dir_is_inside_repo_temp_runtime() -> None:
    run_id = "run-" + "0" * 32
    path = snapshot_dir(run_id)
    assert path == repo_root() / "temp" / "runtime" / run_id / "snapshots"
    assert path.is_relative_to(repo_root() / "temp" / "runtime")
