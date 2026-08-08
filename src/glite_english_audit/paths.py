"""Centralized filesystem locations for private runtime state.

Everything the audit writes lives inside the checkout, under the Git-ignored
``temp/runtime/`` tree. One location rather than two means one thing to
inspect, one thing to delete, and one cleanup path to verify. It also removes
the failure the split design allowed: deleting the checkout used to orphan
private run data in a per-user application directory, where it stayed
indefinitely with nothing left pointing at it.

Layout::

    <repository>/temp/runtime/
    ├── runs/<run-id>/
    │   ├── run-manifest.json
    │   ├── stages/<n>/
    │   ├── logs/
    │   ├── snapshots/        # copies of source data, removed after extraction
    │   └── submission/
    ├── calibration/
    └── config/

Two consequences to keep in mind. Git ignoring this tree is a convention, not
a permission boundary, so the snapshot gates in
:mod:`glite_english_audit.discovery.snapshot_safety` still ask Git whether the
path is really ignored before writing source copies into it. And calibration
history now belongs to the checkout, so a fresh clone starts without it.
"""

import platform
import re
from pathlib import Path

from glite_english_audit.artifacts.enums import OsEnvironment, StageId

RUNTIME_DIR_NAME = "runtime"
"""Subdirectory of ``temp/`` holding every private runtime artifact."""

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


def repo_root() -> Path:
    """The repository root, resolved from this file's location."""
    return Path(__file__).resolve().parent.parent.parent


def runtime_root(*, repo: Path | None = None) -> Path:
    """The private runtime root inside the checkout.

    ``repo`` is injectable for tests; real runs use this repository.
    """
    base = repo if repo is not None else repo_root()
    return base / "temp" / RUNTIME_DIR_NAME


def runs_root(*, repo: Path | None = None) -> Path:
    """Directory holding one subdirectory per run."""
    return runtime_root(repo=repo) / "runs"


def run_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Private state directory for one run."""
    return runs_root(repo=repo) / validate_run_id(run_id)


def stage_dir(
    run_id: str,
    stage: StageId,
    *,
    root: Path | None = None,
    repo: Path | None = None,
) -> Path:
    """Directory holding one stage's current artifacts inside a run.

    ``root`` overrides the runs root for tests.
    """
    base = root / validate_run_id(run_id) if root is not None else run_dir(run_id, repo=repo)
    return base / "stages" / str(int(stage))


def snapshot_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Snapshot directory for one run, inside that run's own directory."""
    return run_dir(run_id, repo=repo) / "snapshots"


def endpoint_config_dir(*, repo: Path | None = None) -> Path:
    """Directory holding operator-provided endpoint configuration."""
    return runtime_root(repo=repo) / "config"


def calibration_history_path(*, repo: Path | None = None) -> Path:
    """Numerical token-calibration history shared across runs in this checkout."""
    return runtime_root(repo=repo) / "calibration" / "local-history.jsonl"
