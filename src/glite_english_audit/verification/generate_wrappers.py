"""CLI: regenerate the Windows-safe skill discovery wrappers.

Run: ``uv run python -m glite_english_audit.verification.generate_wrappers``
Wrappers under ``.claude/skills/`` and ``.codex/skills/`` are generated files;
hand edits are overwritten and flagged by the verifier as drift.
"""

import sys
from pathlib import Path

from glite_english_audit.paths import repo_root
from glite_english_audit.verification.skills import (
    WRAPPER_DIRS,
    parse_skill,
    skills_root,
    wrapper_content,
)


def generate_all(root: Path) -> list[Path]:
    """Write every wrapper; returns the paths written."""
    written: list[Path] = []
    skills_dir = skills_root(root)
    if not skills_dir.is_dir():
        return written
    for directory in sorted(entry for entry in skills_dir.iterdir() if entry.is_dir()):
        parsed, diagnostics = parse_skill(directory)
        if parsed is None:
            details = "; ".join(d.message for d in diagnostics)
            msg = f"cannot generate wrappers for unparsable skill {directory.name!r}: {details}"
            raise ValueError(msg)
        for wrapper_dir in WRAPPER_DIRS:
            content = wrapper_content(parsed, wrapper_dir)
            wrapper_path = root / wrapper_dir / parsed.name / "SKILL.md"
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_text(content, encoding="utf-8")
            written.append(wrapper_path)
    return written


def main() -> int:
    """Regenerate all wrappers under the repository root."""
    written = generate_all(repo_root())
    noun = "file" if len(written) == 1 else "files"
    sys.stdout.write(f"generated {len(written)} wrapper {noun}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
