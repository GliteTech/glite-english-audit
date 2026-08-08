"""Safety gates around the repository-owned snapshot directory.

Source snapshots are the one kind of private data allowed inside the checkout,
under ``temp/runtime/<run-id>/snapshots/`` (specification, 3.6). Before any
snapshot is created, every gate here must pass; on any failure snapshotting
stops before a byte of source content is read. Cleanup is manifest-bounded: it
deletes only files listed in the snapshot manifest, resolved under the run's
snapshot directory, and never follows a path outside it.
"""

import subprocess
from pathlib import Path

from glite_english_audit.artifacts.models import SnapshotManifest
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.paths import repo_root, snapshot_dir

# Directory names that indicate a cloud-synced or roaming location. Git ignore
# rules do not protect against sync clients, so snapshots are refused outright
# when the repository sits under one of these.
_SYNCED_ROOT_MARKERS = {
    "dropbox",
    "onedrive",
    "google drive",
    "googledrive",
    "icloud drive",
    "icloud drive (archive)",
    "cloudstorage",
    "box",
    "syncthing",
}


class SnapshotSafetyError(Exception):
    """A snapshot safety gate failed; snapshotting must not proceed."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def _fail(code: str, message: str) -> SnapshotSafetyError:
    return SnapshotSafetyError(Diagnostic.from_code(code, message))


def _check_no_symlink_components(root: Path, target: Path) -> None:
    """Reject any symlink between the repository root and the target."""
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise _fail(
                "SOURCE_SNAPSHOT_UNSAFE_PATH",
                f"snapshot path component is a symlink: {part!r}",
            )


def _check_not_synced_root(root: Path) -> None:
    for ancestor in (root, *root.parents):
        if ancestor.name.lower() in _SYNCED_ROOT_MARKERS:
            raise _fail(
                "SOURCE_SNAPSHOT_SYNCED_ROOT",
                "the repository sits inside a cloud-synced directory; "
                "clone it to a local, non-synced location before snapshotting",
            )


def _check_git_ignored(root: Path, target: Path) -> None:
    relative = target.relative_to(root).as_posix()
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise _fail(
            "SOURCE_SNAPSHOT_NOT_IGNORED",
            f"Git does not ignore {relative!r}; refusing to create a snapshot there",
        )


def ensure_safe_snapshot_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Validate and create the snapshot directory for one run.

    Gates, in order: the resolved path must sit inside this repository's
    ``temp/runtime`` tree; no component between the repository root and the
    target may be a symlink; the repository must not live under a known
    cloud-synced root; and Git must confirm the path is ignored.

    ``repo`` is injectable for tests; real runs use this repository.
    """
    root = (repo if repo is not None else repo_root()).resolve()
    target = snapshot_dir(run_id, repo=root)
    expected_tree = (root / "temp" / "runtime").resolve()
    if not target.resolve().is_relative_to(expected_tree):
        raise _fail(
            "SOURCE_SNAPSHOT_UNSAFE_PATH",
            "snapshot target resolved outside the repository-owned temp/runtime tree",
        )
    _check_not_synced_root(root)
    _check_git_ignored(root, target)
    target.mkdir(parents=True, exist_ok=True)
    _check_no_symlink_components(root, target)
    return target


def cleanup_snapshot(
    manifest: SnapshotManifest, run_id: str, *, repo: Path | None = None
) -> list[Path]:
    """Delete exactly the files listed in ``manifest`` for this run.

    Returns the deleted paths. Refuses unbounded paths, symlinks, and anything
    that resolves outside the run's snapshot directory. Never touches source
    application data: it only ever operates under ``temp/runtime``.
    """
    base = snapshot_dir(run_id, repo=repo).resolve()
    deleted: list[Path] = []
    for entry in manifest.files:
        unresolved = base / entry.relative_path
        # The symlink check must run on the unresolved path: resolving first
        # would silently follow the link and delete its target instead.
        if unresolved.is_symlink():
            raise _fail(
                "SOURCE_SNAPSHOT_UNSAFE_PATH",
                f"cleanup entry is a symlink: {entry.relative_path!r}",
            )
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(base):
            raise _fail(
                "SOURCE_SNAPSHOT_UNSAFE_PATH",
                f"cleanup entry escapes the snapshot directory: {entry.relative_path!r}",
            )
        if candidate.exists():
            candidate.unlink()
            deleted.append(candidate)
    # Remove now-empty directories bottom-up, staying inside the snapshot dir.
    for directory in sorted(
        (p for p in base.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    return deleted
