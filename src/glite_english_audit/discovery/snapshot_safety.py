"""Safety gates around the repository-owned snapshot directory.

Source snapshots are the one kind of private data allowed inside the checkout,
under ``runtime/runs/<run-id>/snapshots/`` (specification, 3.6). Before any
snapshot is created, every gate here must pass; on any failure snapshotting
stops before a byte of source content is read. Cleanup is manifest-bounded: it
deletes only files listed in the snapshot manifest, resolved under the run's
snapshot directory, and never follows a path outside it.
"""

import subprocess
from pathlib import Path

from glite_english_audit.artifacts.io import ensure_private_dir
from glite_english_audit.artifacts.models import SnapshotManifest
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.paths import RUNTIME_DIR_NAME, repo_root, snapshot_dir

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


def _gated_snapshot_dir(run_id: str, repo: Path | None) -> tuple[Path, Path]:
    """Return the repository root and the gated snapshot path for one run.

    Gates: the run ID must be well formed, because pathlib joins an absolute or
    ``..``-traversing ID away from the repository; the resolved target must sit
    inside this repository's ``runtime`` tree; and no component between
    the repository root and the target may be a symlink, so a planted link
    cannot make foreign files look contained.
    """
    root = (repo if repo is not None else repo_root()).resolve()
    try:
        target = snapshot_dir(run_id, repo=root)
    except ValueError as error:
        raise _fail(
            "SOURCE_SNAPSHOT_UNSAFE_PATH",
            f"snapshot run identifier is not well formed: {run_id!r}",
        ) from error
    expected_tree = (root / RUNTIME_DIR_NAME).resolve()
    if not target.resolve().is_relative_to(expected_tree):
        raise _fail(
            "SOURCE_SNAPSHOT_UNSAFE_PATH",
            "snapshot target resolved outside the repository-owned runtime tree",
        )
    _check_no_symlink_components(root, target)
    return root, target


def ensure_safe_snapshot_dir(run_id: str, *, repo: Path | None = None) -> Path:
    """Validate and create the snapshot directory for one run.

    Gates, in order: the run ID, containment, and symlink checks of
    :func:`_gated_snapshot_dir`; the repository must not live under a known
    cloud-synced root; and Git must confirm the path is ignored.

    ``repo`` is injectable for tests; real runs use this repository.
    """
    root, target = _gated_snapshot_dir(run_id, repo)
    _check_not_synced_root(root)
    _check_git_ignored(root, target)
    # Owner-only, not the umask default. Everything above this line guards
    # where the directory may be; this guards who may read it once it exists.
    # It holds verbatim copies of the user's application data, and a plain
    # mkdir left it world-readable — along with the run directory it creates
    # on the way, which later holds their sentences and every finding about
    # them.
    ensure_private_dir(target)
    _check_no_symlink_components(root, target)
    return target


def cleanup_snapshot(
    manifest: SnapshotManifest, run_id: str, *, repo: Path | None = None
) -> list[Path]:
    """Delete exactly the files listed in ``manifest`` for this run.

    Returns the deleted paths. Re-runs the path gates first: a resumed run
    reaches cleanup without passing through :func:`ensure_safe_snapshot_dir`
    again, so the run ID, containment, and symlink checks must hold here too.
    Refuses unbounded paths, symlinks, and anything that resolves outside the
    run's snapshot directory. Never touches source application data: it only
    ever operates under ``runtime``.
    """
    _, gated = _gated_snapshot_dir(run_id, repo)
    if gated.exists() and not gated.is_dir():
        raise _fail(
            "SOURCE_SNAPSHOT_UNSAFE_PATH",
            "snapshot base is not a directory",
        )
    base = gated.resolve()
    if not gated.exists():
        return []
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
    # A symlinked directory, or one reached through a symlinked parent, is
    # skipped: rmdir through a link removes a directory outside the tree.
    directories = [
        path
        for path in base.rglob("*")
        if path.is_dir() and not path.is_symlink() and path.resolve().is_relative_to(base)
    ]
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    return deleted
