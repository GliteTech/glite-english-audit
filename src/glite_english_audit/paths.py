"""Centralized filesystem locations for private runtime state.

Persistent private run state lives outside the Git checkout (specification,
3.6). Source snapshots are the deliberate exception and live under
``<repository>/temp/runtime/<run-id>/snapshots/``; their safety checks live in
:mod:`glite_english_audit.discovery.snapshot_safety`.
"""

import os
import platform
import re
from pathlib import Path

from glite_english_audit.artifacts.enums import OsEnvironment, StageId

APP_DIR_NAME_MACOS = "Glite English Audit"
APP_DIR_NAME_WINDOWS = "Glite English Audit"
APP_DIR_NAME_XDG = "glite-english-audit"

RUN_ID_PATTERN = re.compile(r"^run-[0-9a-f]{32}$")
"""The only accepted run-identifier shape, produced by ``new_run_id``."""


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` if it is a well-formed run identifier.

    Every path below joins the run ID into a filesystem path, and pathlib
    resolves ``base / "/absolute"`` to ``/absolute`` and keeps ``..`` segments.
    An unvalidated run ID therefore points anywhere on disk, so the format is
    checked at each joining site rather than trusted from the caller.
    """
    if not RUN_ID_PATTERN.fullmatch(run_id):
        msg = f"not a valid run identifier: {run_id!r}"
        raise ValueError(msg)
    return run_id


def detect_os_environment() -> OsEnvironment:
    """Detect the current environment. WSL is distinct from native Linux."""
    system = platform.system()
    if system == "Darwin":
        return OsEnvironment.MACOS
    if system == "Windows":
        return OsEnvironment.WINDOWS
    if system == "Linux":
        release = platform.release().lower()
        if "microsoft" in release:
            return OsEnvironment.WSL
        version_path = Path("/proc/version")
        try:
            if version_path.exists() and "microsoft" in version_path.read_text().lower():
                return OsEnvironment.WSL
        except OSError:
            pass
        return OsEnvironment.LINUX
    msg = f"unsupported operating system: {system}"
    raise RuntimeError(msg)


def runtime_root(environment: OsEnvironment | None = None) -> Path:
    """The per-user private runtime root for the given environment."""
    env = environment if environment is not None else detect_os_environment()
    if env is OsEnvironment.MACOS:
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME_MACOS
    if env is OsEnvironment.WINDOWS:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            msg = "LOCALAPPDATA is not set; cannot locate the Windows runtime root"
            raise RuntimeError(msg)
        return Path(local_app_data) / APP_DIR_NAME_WINDOWS
    # WSL and native Linux both use XDG state on the Linux filesystem. WSL
    # state must never live under /mnt/<drive> (specification, 3.6).
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / APP_DIR_NAME_XDG


def runs_root(environment: OsEnvironment | None = None) -> Path:
    """Directory holding one subdirectory per run."""
    return runtime_root(environment) / "runs"


def run_dir(run_id: str, environment: OsEnvironment | None = None) -> Path:
    """Private state directory for one run."""
    return runs_root(environment) / validate_run_id(run_id)


def stage_dir(
    run_id: str,
    stage: StageId,
    environment: OsEnvironment | None = None,
    *,
    root: Path | None = None,
) -> Path:
    """Directory holding one stage's current artifacts inside a run.

    ``root`` overrides the runs root for tests.
    """
    base = root / validate_run_id(run_id) if root is not None else run_dir(run_id, environment)
    return base / "stages" / str(int(stage))


def endpoint_config_dir(environment: OsEnvironment | None = None) -> Path:
    """Directory holding operator-provided endpoint configuration."""
    return runtime_root(environment) / "config"


def calibration_history_path(environment: OsEnvironment | None = None) -> Path:
    """Numerical token-calibration history shared across runs on this machine."""
    return runtime_root(environment) / "calibration" / "local-history.jsonl"


def repo_root() -> Path:
    """The repository root, resolved from this file's location."""
    return Path(__file__).resolve().parent.parent.parent


def snapshot_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Repository-owned snapshot directory for one run (Git-ignored).

    ``repo`` is injectable for tests; real runs use this repository.
    """
    base = repo if repo is not None else repo_root()
    return base / "temp" / "runtime" / validate_run_id(run_id) / "snapshots"
