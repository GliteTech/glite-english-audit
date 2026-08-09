"""Centralized filesystem locations for private runtime state.

Everything the audit writes lives inside the checkout, under the Git-ignored
``runtime/`` tree. One location rather than two means one thing to inspect, one
thing to delete, and one cleanup path to verify. It also removes the failure
the split design allowed: deleting the checkout used to orphan private run data
in a per-user application directory, where it stayed indefinitely with nothing
left pointing at it.

``runtime/`` is deliberately not under ``temp/``. That tree holds development
material — research notes, calibration corpora, design references — and is
deleted before the public release as though it never existed. This one is the
opposite: it is what the released product creates on a user's machine the first
time they run an audit.

Layout::

    <repository>/runtime/
    ├── runs/<run-id>/
    │   ├── run-manifest.json
    │   ├── stages/<n>-<name>/
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
"""Top-level directory holding every private runtime artifact."""

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
    return base / RUNTIME_DIR_NAME


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
    stages = base / "stages"
    # Runs written before stages carried names keep the numeric layout, so a
    # rename does not strand a resumable run. One numeric directory anywhere
    # settles it for the whole run: a run half in each naming would be worse
    # than either.
    if (stages / str(int(stage))).exists() or _has_numeric_stages(stages):
        return stages / str(int(stage))
    return stages / stage_dir_name(stage)


def stage_dir_name(stage: StageId) -> str:
    """The on-disk directory name for one stage: ``4-plain-findings``.

    The number leads because it is what the specification, the skills, and the
    manifest all call this stage, and because it sorts. The name follows
    because a person looking inside a run should not have to hold a table of
    nine numbers in their head to know what they are reading — and this project
    is built around that folder being the one place to inspect.
    """
    return f"{int(stage)}-{stage.name.lower().replace('_', '-')}"


def _has_numeric_stages(stages: Path) -> bool:
    """Whether this run was written under the older numeric layout."""
    try:
        return any(child.name.isdigit() for child in stages.iterdir() if child.is_dir())
    except OSError:
        return False


def snapshot_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Snapshot directory for one run, inside that run's own directory."""
    return run_dir(run_id, repo=repo) / "snapshots"


def pending_inventory_dir(*, repo: Path | None = None) -> Path:
    """Where discovery leaves its inventory before a run exists.

    Discovery runs first: the user has to see what was found before choosing
    sources, and only then is a run created. So stage 0 has nowhere run-scoped
    to write, and its output waits here until ``start_run`` adopts it.
    """
    return runtime_root(repo=repo) / "inventory"


def endpoint_config_dir(*, repo: Path | None = None) -> Path:
    """Directory holding operator-provided endpoint configuration."""
    return runtime_root(repo=repo) / "config"


def calibration_history_path(*, repo: Path | None = None) -> Path:
    """Numerical token-calibration history shared across runs in this checkout."""
    return runtime_root(repo=repo) / "calibration" / "local-history.jsonl"
