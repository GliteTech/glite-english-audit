"""Every private runtime path lives inside the checkout's ignored tree."""

import subprocess
import sys
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import OsEnvironment, StageId
from glite_english_audit.artifacts.hashing import new_run_id
from glite_english_audit.paths import (
    calibration_history_path,
    detect_os_environment,
    endpoint_config_dir,
    repo_root,
    run_dir,
    runs_root,
    runtime_root,
    snapshot_dir,
    stage_dir,
    stage_dir_name,
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
    assert runs_root(repo=tmp_path) == root / "runs"
    assert run_dir(_RUN_ID, repo=tmp_path) == root / "runs" / _RUN_ID
    assert snapshot_dir(_RUN_ID, repo=tmp_path) == root / "runs" / _RUN_ID / "snapshots"
    assert endpoint_config_dir(repo=tmp_path) == root / "config"
    assert calibration_history_path(repo=tmp_path) == root / "calibration" / "local-history.jsonl"
    assert (
        stage_dir(_RUN_ID, StageId.PLAIN_FINDINGS, repo=tmp_path)
        == root / "runs" / _RUN_ID / "stages" / "4-plain-findings"
    )


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
        stage_dir(_RUN_ID, StageId.ELIGIBLE_ENGLISH, repo=root),
    ):
        assert path.is_relative_to(root)


def test_git_ignores_the_whole_runtime_tree() -> None:
    # Ignoring is a convention rather than a permission boundary, but it must
    # at least hold: ask Git itself rather than trusting the .gitignore text.
    root = repo_root()
    for relative in (
        f"runtime/runs/{_RUN_ID}/run-manifest.json",
        f"runtime/runs/{_RUN_ID}/snapshots/session.jsonl",
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
def test_stage_dir_rejects_malformed_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run identifier"):
        stage_dir(run_id, StageId.SOURCE_INVENTORY, root=tmp_path)


def test_a_stage_directory_names_its_stage(tmp_path: Path) -> None:
    """A person looking inside a run should not need a table of nine numbers.

    The number leads because it is what the specification, the skills, and the
    manifest all call the stage, and because it sorts. The name follows because
    this folder is the one place the design asks people to inspect.
    """
    assert stage_dir_name(StageId.SOURCE_INVENTORY) == "0-source-inventory"
    assert stage_dir_name(StageId.PRIVATE_MISTAKES) == "5-private-mistakes"
    assert stage_dir_name(StageId.REVIEWED_SUBMISSION) == "8-reviewed-submission"
    names = [stage_dir_name(stage) for stage in StageId]
    assert names == sorted(names), "the number must lead so the directories sort in order"


def test_a_run_written_under_the_numeric_layout_still_resolves(tmp_path: Path) -> None:
    """Renaming must not strand a resumable run.

    Runs already on disk have stages/4; new code looking for
    stages/4-plain-findings would find an empty directory and conclude the
    stage had never run.
    """
    stages = tmp_path / "runs" / _RUN_ID / "stages"
    (stages / "4").mkdir(parents=True)
    assert stage_dir(_RUN_ID, StageId.PLAIN_FINDINGS, root=tmp_path / "runs").name == "4"


def test_one_numeric_stage_settles_the_layout_for_the_whole_run(tmp_path: Path) -> None:
    # A run half in each naming would be worse than either, so the presence of
    # any numeric directory decides for stages that do not exist yet.
    stages = tmp_path / "runs" / _RUN_ID / "stages"
    (stages / "2").mkdir(parents=True)
    assert stage_dir(_RUN_ID, StageId.SAFE_RECORDS, root=tmp_path / "runs").name == "6"


def test_a_fresh_run_uses_names(tmp_path: Path) -> None:
    (tmp_path / "runs" / _RUN_ID / "stages").mkdir(parents=True)
    resolved = stage_dir(_RUN_ID, StageId.SAFE_RECORDS, root=tmp_path / "runs")
    assert resolved.name == "6-safe-records"
