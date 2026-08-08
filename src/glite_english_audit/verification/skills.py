"""Deterministic skill checks shared by the verifier and wrapper generator.

The rules implement ``specifications/agent_skills_specification.md``: one
canonical skill per directory under ``skills/``, strict frontmatter and body
structure, and byte-exact generated wrappers under ``.claude/skills/`` and
``.codex/skills/`` (no symlinks, so Windows checkouts work unchanged).
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from glite_english_audit.diagnostics.codes import Diagnostic

REQUIRED_SECTIONS = (
    "## Goal",
    "## Inputs",
    "## Context",
    "## Steps",
    "## Done When",
    "## Forbidden",
)
EMPHASIS_BUDGET = 5
WRAPPER_DIRS = (".claude/skills", ".codex/skills")

_VERSION_PATTERN = re.compile(r"^\*\*Version\*\*: (\d+)$", re.MULTILINE)
_TITLE_PATTERN = re.compile(r"^# ", re.MULTILINE)
_EMPHASIS_PATTERN = re.compile(r"\b(MUST|NEVER|CRITICAL)\b")
_LOCAL_REFERENCE_PATTERN = re.compile(
    r"`((?:skills|src|schemas|specifications|styleguide|fixtures)/[^`\n]+?)`"
)


@dataclass(frozen=True)
class ParsedSkill:
    """One canonical skill file, split into frontmatter and body."""

    name: str
    directory: Path
    frontmatter_text: str
    frontmatter: dict[str, object]
    body: str


def skills_root(repo_root: Path) -> Path:
    """The canonical skills directory."""
    return repo_root / "skills"


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split a SKILL.md into (frontmatter text, body). None when malformed."""
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return None
    return text[4:closing], text[closing + len("\n---\n") :]


def parse_skill(directory: Path) -> tuple[ParsedSkill | None, list[Diagnostic]]:
    """Parse one canonical skill directory, collecting structural diagnostics."""
    diagnostics: list[Diagnostic] = []
    slug = directory.name
    skill_path = directory / "SKILL.md"
    if not skill_path.is_file() or not skill_path.read_text(encoding="utf-8").strip():
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_MISSING_FILE",
                f"skills/{slug}/SKILL.md is missing or empty",
                item_ref=slug,
            )
        )
        return None, diagnostics
    text = skill_path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_FRONTMATTER_INVALID",
                f"skills/{slug}/SKILL.md frontmatter is missing or not delimited by ---",
                item_ref=slug,
            )
        )
        return None, diagnostics
    frontmatter_text, body = split
    try:
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        loaded = None
    if not isinstance(loaded, dict):
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_FRONTMATTER_INVALID",
                f"skills/{slug}/SKILL.md frontmatter is not a YAML mapping",
                item_ref=slug,
            )
        )
        return None, diagnostics
    frontmatter: dict[str, object] = {str(key): value for key, value in loaded.items()}
    parsed = ParsedSkill(
        name=slug,
        directory=directory,
        frontmatter_text=frontmatter_text,
        frontmatter=frontmatter,
        body=body,
    )
    return parsed, diagnostics


def check_skill(parsed: ParsedSkill, repo_root: Path) -> list[Diagnostic]:
    """Structural rules for one parsed canonical skill."""
    diagnostics: list[Diagnostic] = []
    slug = parsed.name

    name = parsed.frontmatter.get("name")
    description = parsed.frontmatter.get("description")
    if not isinstance(name, str) or not isinstance(description, str) or not description.strip():
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_FRONTMATTER_INVALID",
                f"skills/{slug}: frontmatter must contain string 'name' and 'description'",
                item_ref=slug,
            )
        )
    elif name != slug:
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_NAME_MISMATCH",
                f"skills/{slug}: frontmatter name {name!r} does not match the directory",
                item_ref=slug,
            )
        )

    if len(_TITLE_PATTERN.findall(parsed.body)) != 1:
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_TITLE_COUNT",
                f"skills/{slug}: body must contain exactly one top-level '#' title",
                item_ref=slug,
            )
        )

    if not _VERSION_PATTERN.search(parsed.body):
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_VERSION_INVALID",
                f"skills/{slug}: body must contain '**Version**: N' with a plain integer",
                item_ref=slug,
            )
        )

    for section in REQUIRED_SECTIONS:
        if not re.search(rf"^{re.escape(section)}$", parsed.body, re.MULTILINE):
            diagnostics.append(
                Diagnostic.from_code(
                    "SKILL_SECTION_MISSING",
                    f"skills/{slug}: required section {section!r} is missing",
                    item_ref=slug,
                )
            )

    emphasis_count = len(_EMPHASIS_PATTERN.findall(parsed.body))
    if emphasis_count > EMPHASIS_BUDGET:
        diagnostics.append(
            Diagnostic.from_code(
                "SKILL_EMPHASIS_BUDGET_EXCEEDED",
                f"skills/{slug}: {emphasis_count} emphasized MUST/NEVER/CRITICAL rules "
                f"exceed the budget of {EMPHASIS_BUDGET}",
                item_ref=slug,
            )
        )

    for match in _LOCAL_REFERENCE_PATTERN.finditer(parsed.body):
        reference = match.group(1).split("#", maxsplit=1)[0].rstrip("/")
        if any(char in reference for char in "*<>"):
            continue
        if not (repo_root / reference).exists():
            diagnostics.append(
                Diagnostic.from_code(
                    "SKILL_REFERENCED_FILE_MISSING",
                    f"skills/{slug}: referenced local file does not exist: {reference!r}",
                    item_ref=slug,
                )
            )

    return diagnostics


def wrapper_content(parsed: ParsedSkill) -> str:
    """The exact expected content of a generated discovery wrapper."""
    return (
        f"---\n{parsed.frontmatter_text}\n---\n\n"
        f"# {parsed.name} wrapper\n\n"
        "Generated wrapper. Do not edit. Read and follow the canonical skill instructions in\n"
        f"`skills/{parsed.name}/SKILL.md` exactly.\n"
    )


def check_wrappers(parsed: ParsedSkill, repo_root: Path) -> list[Diagnostic]:
    """Byte-exact wrapper consistency for one skill."""
    diagnostics: list[Diagnostic] = []
    expected = wrapper_content(parsed)
    for wrapper_dir in WRAPPER_DIRS:
        wrapper_path = repo_root / wrapper_dir / parsed.name / "SKILL.md"
        if not wrapper_path.is_file():
            diagnostics.append(
                Diagnostic.from_code(
                    "SKILL_WRAPPER_MISSING",
                    f"{wrapper_dir}/{parsed.name}/SKILL.md is missing; "
                    "run: uv run python -m glite_english_audit.verification.generate_wrappers",
                    item_ref=parsed.name,
                )
            )
            continue
        if wrapper_path.read_text(encoding="utf-8") != expected:
            diagnostics.append(
                Diagnostic.from_code(
                    "SKILL_WRAPPER_DRIFT",
                    f"{wrapper_dir}/{parsed.name}/SKILL.md drifted from the canonical skill; "
                    "run: uv run python -m glite_english_audit.verification.generate_wrappers",
                    item_ref=parsed.name,
                )
            )
    return diagnostics


def check_orphan_wrappers(repo_root: Path) -> list[Diagnostic]:
    """Wrappers whose canonical skill no longer exists."""
    diagnostics: list[Diagnostic] = []
    canonical = (
        {
            entry.name
            for entry in skills_root(repo_root).iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        }
        if skills_root(repo_root).is_dir()
        else set()
    )
    for wrapper_dir in WRAPPER_DIRS:
        base = repo_root / wrapper_dir
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.is_dir() and entry.name not in canonical:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SKILL_WRAPPER_DRIFT",
                        f"{wrapper_dir}/{entry.name} has no canonical skill under skills/",
                        item_ref=entry.name,
                    )
                )
    return diagnostics


def verify_all_skills(repo_root: Path) -> list[Diagnostic]:
    """Run every deterministic skill check across the repository."""
    diagnostics: list[Diagnostic] = []
    root = skills_root(repo_root)
    if root.is_dir():
        for directory in sorted(entry for entry in root.iterdir() if entry.is_dir()):
            parsed, parse_diagnostics = parse_skill(directory)
            diagnostics.extend(parse_diagnostics)
            if parsed is not None:
                diagnostics.extend(check_skill(parsed, repo_root))
                diagnostics.extend(check_wrappers(parsed, repo_root))
    diagnostics.extend(check_orphan_wrappers(repo_root))
    return diagnostics
