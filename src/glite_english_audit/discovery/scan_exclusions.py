"""Directories a filename-scanning adapter must never treat as user data.

Most adapters read one fixed application store. An adapter whose source has no
central location (Aider writes history beside each project) must scan instead,
and a scan can wander into data that only looks like user history:

- This project's own synthetic fixtures. On a contributor's machine those
  files sit under the checkout and would otherwise be ingested as if the
  contributor had written them, corrupting a real audit's corpus and counts.
- This project's private runtime tree, which holds snapshot copies of source
  data. Ingesting a snapshot would double-count the same production event.

Both are pruned here so every scanning adapter shares one rule.
"""

import json
from pathlib import Path

from glite_english_audit.paths import repo_root

FIXTURE_MARKER_NAME = "fixture.json"

# The marker is a small project-owned declaration; anything larger is not ours
# and is left unread.
_MAX_MARKER_BYTES = 64 * 1024


def is_synthetic_fixture_dir(directory: Path) -> bool:
    """True when ``directory`` declares itself synthetic test data."""
    marker = directory / FIXTURE_MARKER_NAME
    try:
        if not marker.is_file() or marker.stat().st_size > _MAX_MARKER_BYTES:
            return False
        declaration = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(declaration, dict) and declaration.get("synthetic") is True


def audit_owned_roots(repo: Path | None = None) -> frozenset[Path]:
    """Private project trees a scan must never enter.

    Only the runtime tree is excluded by path. Synthetic fixtures are found by
    their marker instead, so a test may still point a scan directly at a
    fixture home while a scan of a real home stops at the fixture boundary.
    """
    root = repo if repo is not None else repo_root()
    return frozenset({root / "temp", root / "runtime"})


def should_prune_scan_dir(directory: Path, *, audit_roots: frozenset[Path]) -> bool:
    """True when a scanning adapter must skip ``directory`` and its subtree."""
    if any(directory == root or directory.is_relative_to(root) for root in audit_roots):
        return True
    return is_synthetic_fixture_dir(directory)
