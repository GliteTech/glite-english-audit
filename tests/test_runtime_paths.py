"""Every private runtime path lives inside the checkout's ignored tree."""

import subprocess
import sys
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import OsEnvironment, StepId
from glite_english_audit.artifacts.hashing import new_run_id
from glite_english_audit.paths import (
    calibration_history_path,
    detect_os_environment,
    endpoint_config_dir,
    inventory_path,
    repo_root,
    run_dir,
    runs_root,
    runtime_root,
    snapshot_dir,
    snapshot_manifest_dir,
    step_dir,
    step_dir_name,
    submission_dir,
    validate_run_id,
)

_RUN_ID = "run-" + "0" * 32


def test_runtime_root_is_inside_the_checkout(tmp_path: Path) -> None:
    assert runtime_root(repo=tmp_path) == tmp_path / "runtime"
    assert runtime_root().is_relative_to(repo_root())


def test_runtime_root_is_identical_on_every_platform(tmp_path: Path) -> None:
    # The layout no longer branches per operating system, so a run made on one
    # platform is readable on any other and there is one path to audit.
    assert runtime_root(repo=tmp_path) == tmp_path / "runtime"


def test_every_private_location_nests_under_the_runtime_root(tmp_path: Path) -> None:
    root = runtime_root(repo=tmp_path)
    run = root / "runs" / _RUN_ID
    assert runs_root(repo=tmp_path) == root / "runs"
    assert run_dir(_RUN_ID, repo=tmp_path) == run
    assert snapshot_dir(_RUN_ID, repo=tmp_path) == run / "snapshots"
    assert endpoint_config_dir(repo=tmp_path) == root / "config"
    assert calibration_history_path(repo=tmp_path) == root / "calibration" / "local-history.jsonl"
    assert step_dir(_RUN_ID, StepId.D_MISTAKES, repo=tmp_path) == run / "steps" / "d-mistakes"
    # The inventory, the snapshot manifests and the reviewed submission are not
    # per-session, so they are no longer steps and step_dir no longer covers
    # them. Each needs naming here or a private location goes unchecked.
    assert inventory_path(_RUN_ID, repo=tmp_path) == run / "source-inventory.json"
    assert snapshot_manifest_dir(_RUN_ID, repo=tmp_path) == run / "snapshot-manifests"
    assert submission_dir(_RUN_ID, repo=tmp_path) == run / "submission"


def test_deleting_the_checkout_removes_every_private_location(tmp_path: Path) -> None:
    # The point of the in-repo layout: nothing survives the checkout, so no
    # private run data can be orphaned somewhere the user never looks.
    root = tmp_path.resolve()
    for path in (
        runtime_root(repo=root),
        runs_root(repo=root),
        run_dir(_RUN_ID, repo=root),
        snapshot_dir(_RUN_ID, repo=root),
        endpoint_config_dir(repo=root),
        calibration_history_path(repo=root),
        step_dir(_RUN_ID, StepId.C_AUTHORED, repo=root),
        inventory_path(_RUN_ID, repo=root),
        snapshot_manifest_dir(_RUN_ID, repo=root),
        submission_dir(_RUN_ID, repo=root),
    ):
        assert path.is_relative_to(root)


def test_git_ignores_the_whole_runtime_tree() -> None:
    # Ignoring is a convention rather than a permission boundary, but it must
    # at least hold: ask Git itself rather than trusting the .gitignore text.
    root = repo_root()
    for relative in (
        f"runtime/runs/{_RUN_ID}/run-manifest.json",
        f"runtime/runs/{_RUN_ID}/snapshots/session.jsonl",
        # A step's per-session files hold the user's own text, so they are the
        # part of the tree it would cost the most to commit by accident.
        f"runtime/runs/{_RUN_ID}/steps/a-collected/session-0001.jsonl",
        "runtime/calibration/local-history.jsonl",
        "runtime/config/submission-endpoint.json",
    ):
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
            check=False,
            capture_output=True,
        )
        assert result.returncode == 0, f"Git does not ignore {relative}"


@pytest.mark.skipif(sys.platform != "darwin", reason="detection test targets this macOS machine")
def test_detect_os_environment_returns_macos() -> None:
    assert detect_os_environment() is OsEnvironment.MACOS


def test_repo_root_is_the_repository_checkout() -> None:
    root = repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "glite_english_audit").is_dir()


_MALFORMED_RUN_IDS = [
    "",
    "run-does-not-exist",
    "run-" + "0" * 31,
    "run-" + "0" * 33,
    "RUN-" + "0" * 32,
    "run-" + "G" * 32,
    "../../victim",
    "run-" + "0" * 32 + "/../..",
    "/absolute",
]


@pytest.mark.parametrize("run_id", _MALFORMED_RUN_IDS)
def test_validate_run_id_rejects_malformed_identifiers(run_id: str) -> None:
    with pytest.raises(ValueError, match="run identifier"):
        validate_run_id(run_id)


def test_validate_run_id_accepts_the_generated_format() -> None:
    generated = new_run_id()
    assert validate_run_id(generated) == generated


@pytest.mark.parametrize("run_id", _MALFORMED_RUN_IDS)
def test_snapshot_dir_rejects_malformed_run_id(run_id: str) -> None:
    # pathlib joins an absolute or traversing run ID away from the repository,
    # so every path-joining site must reject it before a path exists.
    with pytest.raises(ValueError, match="run identifier"):
        snapshot_dir(run_id)


@pytest.mark.parametrize("run_id", _MALFORMED_RUN_IDS)
def test_run_dir_rejects_malformed_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run identifier"):
        run_dir(run_id, repo=tmp_path)


@pytest.mark.parametrize("run_id", _MALFORMED_RUN_IDS)
def test_step_dir_rejects_malformed_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run identifier"):
        step_dir(run_id, StepId.A_COLLECTED, root=tmp_path)


def test_a_step_directory_names_its_step() -> None:
    """A person looking inside a run should not need a table to read it.

    The letter leads because it is what the owner calls the step, and because
    it sorts in pipeline order. The name follows because this folder is the one
    place the design asks people to inspect.
    """
    assert step_dir_name(StepId.A_COLLECTED) == "a-collected"
    assert step_dir_name(StepId.B_DEDUPLICATED) == "b-deduplicated"
    assert step_dir_name(StepId.C_AUTHORED) == "c-authored"
    assert step_dir_name(StepId.D_MISTAKES) == "d-mistakes"
    assert step_dir_name(StepId.E_VERIFIED) == "e-verified"
    names = [step_dir_name(step) for step in StepId]
    assert names == sorted(names), "the letter must lead so the directories sort in pipeline order"


def test_every_step_gets_its_own_directory(tmp_path: Path) -> None:
    # One session is one file that keeps its name through every step, so two
    # steps sharing a directory would have each overwrite the other's output.
    directories = {step_dir(_RUN_ID, step, root=tmp_path) for step in StepId}
    assert len(directories) == len(StepId)


def test_a_leftover_numeric_stage_directory_does_not_divert_a_step(tmp_path: Path) -> None:
    """The five-step layout is the only layout the code reads.

    Nine-step runs cannot be resumed as five-step runs — steps 4, 5 and 6 all
    became step d, and step d has to produce what none of them produced alone —
    so a stale ``steps/`` tree left beside a run must not pull a step's files
    back out of ``steps/``.
    """
    runs = tmp_path / "runs"
    (runs / _RUN_ID / "steps" / "4").mkdir(parents=True)
    resolved = step_dir(_RUN_ID, StepId.D_MISTAKES, root=runs)
    assert resolved == runs / _RUN_ID / "steps" / "d-mistakes"
