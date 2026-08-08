"""Snapshot safety gates and manifest-bounded cleanup against a real Git repo."""

import shutil
import subprocess
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import sha256_hex
from glite_english_audit.artifacts.models import SnapshotFileEntry, SnapshotManifest
from glite_english_audit.discovery.snapshot_safety import (
    SnapshotSafetyError,
    cleanup_snapshot,
    ensure_safe_snapshot_dir,
)

_RUN_ID = "run-" + "0" * 32


def _git_repo(path: Path, *, gitignore: str = "runtime/\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / ".gitignore").write_text(gitignore, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Synthetic Tester",
            "-c",
            "user.email=tester@example.com",
            "commit",
            "--quiet",
            "-m",
            "add gitignore",
        ],
        check=True,
    )
    return path


def _manifest(entries: list[SnapshotFileEntry]) -> SnapshotManifest:
    return SnapshotManifest(
        envelope=ArtifactEnvelope(
            schema_name="snapshot_manifest",
            schema_version=1,
            artifact_id="art-" + "11" * 16,
            run_id=_RUN_ID,
            stage_id=StageId.SOURCE_SNAPSHOTS,
            producer_name="test-factory",
            producer_version="1.0.0",
            created_at=utc_now(),
        ),
        adapter_id="claude_code",
        instance_key="instance-1",
        snapshot_relative_dir=f"runtime/{_RUN_ID}/snapshots",
        files=entries,
    )


def _entry(relative_path: str, content: bytes = b"synthetic") -> SnapshotFileEntry:
    return SnapshotFileEntry(
        relative_path=relative_path,
        size_bytes=len(content),
        sha256=sha256_hex(content),
    )


def test_ensure_safe_snapshot_dir_succeeds(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    target = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    assert target.is_dir()
    assert target == repo.resolve() / "runtime" / "runs" / _RUN_ID / "snapshots"


def test_ensure_safe_snapshot_dir_fails_when_not_ignored(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout", gitignore="docs/\n")
    with pytest.raises(SnapshotSafetyError) as excinfo:
        ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_NOT_IGNORED"
    assert not (repo / "runtime" / "runs" / _RUN_ID / "snapshots").exists()


def test_ensure_safe_snapshot_dir_fails_under_synced_root(tmp_path: Path) -> None:
    repo = tmp_path / "Dropbox" / "checkout"
    repo.mkdir(parents=True)
    with pytest.raises(SnapshotSafetyError) as excinfo:
        ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_SYNCED_ROOT"
    assert not (repo / "runtime").exists()


def test_cleanup_deletes_exactly_manifest_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    (base / "a.txt").write_bytes(b"synthetic")
    (base / "sub").mkdir()
    (base / "sub" / "b.txt").write_bytes(b"synthetic")
    (base / "keep.txt").write_bytes(b"undeclared")

    manifest = _manifest([_entry("a.txt"), _entry("sub/b.txt")])
    deleted = cleanup_snapshot(manifest, _RUN_ID, repo=repo)

    assert {path.name for path in deleted} == {"a.txt", "b.txt"}
    assert not (base / "a.txt").exists()
    assert not (base / "sub").exists()  # emptied directory removed
    assert (base / "keep.txt").exists()  # undeclared file untouched


def test_cleanup_never_removes_a_directory_through_a_symlink(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    victim = tmp_path / "empty-real-dir"
    victim.mkdir()
    (base / "link-dir").symlink_to(victim, target_is_directory=True)
    (base / "a.txt").write_bytes(b"synthetic")

    deleted = cleanup_snapshot(_manifest([_entry("a.txt")]), _RUN_ID, repo=repo)

    assert {path.name for path in deleted} == {"a.txt"}
    assert victim.is_dir()
    assert (base / "link-dir").is_symlink()


def test_cleanup_tolerates_already_missing_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    manifest = _manifest([_entry("never-written.txt")])
    assert cleanup_snapshot(manifest, _RUN_ID, repo=repo) == []


def test_cleanup_refuses_parent_escape(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    outside = base.parent / "outside.txt"
    outside.write_bytes(b"private")

    # The model itself rejects '..'; cleanup must still refuse one smuggled in.
    escape = SnapshotFileEntry.model_construct(
        relative_path="../outside.txt",
        size_bytes=7,
        sha256="0" * 64,
    )
    manifest = _manifest([escape])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, _RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert outside.exists()


def test_cleanup_refuses_symlink_out_of_snapshot(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    target = tmp_path / "private-target.txt"
    target.write_bytes(b"private")
    (base / "link.txt").symlink_to(target)

    manifest = _manifest([_entry("link.txt")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, _RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert target.exists()


def test_snapshot_entry_model_rejects_unbounded_paths() -> None:
    with pytest.raises(ValueError, match="relative"):
        _entry("../escape.txt")
    with pytest.raises(ValueError, match="relative"):
        _entry("/absolute.txt")


@pytest.mark.parametrize(
    "relative_path",
    [
        "..\\..\\etc\\passwd",
        "sub\\..\\..\\escape.txt",
        "\\windows-absolute.txt",
        "C:/drive-absolute.txt",
        "c:relative-to-drive.txt",
        "\\\\server\\share\\file.txt",
        "",
        ".",
    ],
)
def test_snapshot_entry_model_rejects_windows_unbounded_paths(relative_path: str) -> None:
    # Windows is a supported platform: a backslash is a separator there, so
    # '..\\..' escapes the snapshot directory exactly like '../..' on POSIX.
    with pytest.raises(ValueError, match="relative"):
        _entry(relative_path)


def _victim_history(directory: Path) -> Path:
    """A file standing in for real coding-agent history outside the snapshot."""
    directory.mkdir(parents=True, exist_ok=True)
    victim = directory / "history.jsonl"
    victim.write_bytes(b"real coding-agent history")
    return victim


def test_cleanup_refuses_symlinked_snapshot_directory(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    victim = _victim_history(tmp_path / "real-history")
    # A resumed run re-runs cleanup without re-running the creation gates.
    base.rmdir()
    base.symlink_to(victim.parent, target_is_directory=True)

    manifest = _manifest([_entry("history.jsonl", b"real coding-agent history")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, _RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert victim.exists()


def test_cleanup_refuses_symlinked_run_directory(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    victim = _victim_history(tmp_path / "real-history" / "snapshots")
    run_directory = repo.resolve() / "runtime" / "runs" / _RUN_ID
    shutil.rmtree(run_directory)
    run_directory.symlink_to(victim.parent.parent, target_is_directory=True)

    manifest = _manifest([_entry("history.jsonl", b"real coding-agent history")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, _RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert victim.exists()


def test_cleanup_refuses_absolute_run_id(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    victim = _victim_history(tmp_path / "outside" / "snapshots")

    manifest = _manifest([_entry("history.jsonl", b"real coding-agent history")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, str(tmp_path / "outside"), repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert victim.exists()


def test_cleanup_refuses_traversing_run_id(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    victim = _victim_history(repo / "victim" / "snapshots")

    manifest = _manifest([_entry("history.jsonl", b"real coding-agent history")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, "../../victim", repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
    assert victim.exists()


@pytest.mark.parametrize("run_id", ["../../victim", "run-not-hex", "RUN-" + "0" * 32, ""])
def test_ensure_safe_snapshot_dir_rejects_malformed_run_id(tmp_path: Path, run_id: str) -> None:
    repo = _git_repo(tmp_path / "checkout")
    with pytest.raises(SnapshotSafetyError) as excinfo:
        ensure_safe_snapshot_dir(run_id, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"


def test_cleanup_refuses_non_directory_base(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "checkout")
    base = ensure_safe_snapshot_dir(_RUN_ID, repo=repo)
    base.rmdir()
    base.write_bytes(b"not a directory")

    manifest = _manifest([_entry("history.jsonl")])
    with pytest.raises(SnapshotSafetyError) as excinfo:
        cleanup_snapshot(manifest, _RUN_ID, repo=repo)
    assert excinfo.value.diagnostic.code == "SOURCE_SNAPSHOT_UNSAFE_PATH"
