"""Deterministic skill verification: structure rules and wrapper consistency."""

from pathlib import Path

import pytest

from glite_english_audit.diagnostics.codes import Diagnostic, Severity
from glite_english_audit.verification.generate_wrappers import generate_all
from glite_english_audit.verification.skills import (
    OUTPUT_FORMAT_SECTION,
    REQUIRED_SECTIONS,
    check_skill,
    check_wrappers,
    parse_skill,
    verify_all_skills,
    wrapper_content,
)

_FRONTMATTER = """\
---
name: {name}
description: Synthetic test skill that checks structural rules.
---
"""

_VALID_BODY = """\
# Demo skill

**Version**: 1

## Goal

State the goal.

## Inputs

List the inputs.

## Context

Give the context.

## Steps

1. Do the work.

## Done When

The output exists.

## Forbidden

Do not skip checks.

## Output Format

One JSON object per line.
"""


def _write_skill(
    root: Path,
    name: str,
    *,
    body: str = _VALID_BODY,
    frontmatter_name: str | None = None,
) -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    frontmatter = _FRONTMATTER.format(name=frontmatter_name if frontmatter_name else name)
    (directory / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    return directory


def _codes(diagnostics: list[Diagnostic]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _check(root: Path, name: str) -> list[Diagnostic]:
    parsed, diagnostics = parse_skill(root / "skills" / name)
    assert parsed is not None
    return diagnostics + check_skill(parsed, root)


def test_missing_skill_file(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo-skill"
    directory.mkdir(parents=True)
    parsed, diagnostics = parse_skill(directory)
    assert parsed is None
    assert _codes(diagnostics) == ["SKILL_MISSING_FILE"]


def test_empty_skill_file(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("   \n", encoding="utf-8")
    parsed, diagnostics = parse_skill(directory)
    assert parsed is None
    assert _codes(diagnostics) == ["SKILL_MISSING_FILE"]


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all\n\n# Title\n",
        "---\nname: demo-skill\n",  # unterminated frontmatter
        "---\n- one\n- two\n---\n\n# Title\n",  # frontmatter is a list, not a mapping
    ],
)
def test_bad_frontmatter(tmp_path: Path, text: str) -> None:
    directory = tmp_path / "skills" / "demo-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    parsed, diagnostics = parse_skill(directory)
    assert parsed is None
    assert _codes(diagnostics) == ["SKILL_FRONTMATTER_INVALID"]


def test_frontmatter_without_description(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n" + _VALID_BODY, encoding="utf-8"
    )
    assert "SKILL_FRONTMATTER_INVALID" in _codes(_check(tmp_path, "demo-skill"))


def test_name_mismatch(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo-skill", frontmatter_name="other-skill")
    assert "SKILL_NAME_MISMATCH" in _codes(_check(tmp_path, "demo-skill"))


def test_missing_version(tmp_path: Path) -> None:
    body = _VALID_BODY.replace("**Version**: 1\n\n", "")
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_VERSION_INVALID" in _codes(_check(tmp_path, "demo-skill"))


def test_non_integer_version(tmp_path: Path) -> None:
    body = _VALID_BODY.replace("**Version**: 1", "**Version**: 1.2")
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_VERSION_INVALID" in _codes(_check(tmp_path, "demo-skill"))


def test_zero_titles(tmp_path: Path) -> None:
    body = _VALID_BODY.replace("# Demo skill\n", "Demo skill\n")
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_TITLE_COUNT" in _codes(_check(tmp_path, "demo-skill"))


def test_two_titles(tmp_path: Path) -> None:
    body = _VALID_BODY + "\n# Second title\n"
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_TITLE_COUNT" in _codes(_check(tmp_path, "demo-skill"))


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_each_missing_required_section(tmp_path: Path, section: str) -> None:
    body = _VALID_BODY.replace(f"{section}\n", "## Renamed Section\n")
    _write_skill(tmp_path, "demo-skill", body=body)
    diagnostics = _check(tmp_path, "demo-skill")
    missing = [d for d in diagnostics if d.code == "SKILL_SECTION_MISSING"]
    assert len(missing) == 1
    assert section in missing[0].message


def test_missing_output_format_section(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(f"{OUTPUT_FORMAT_SECTION}\n", "## Renamed Section\n")
    _write_skill(tmp_path, "demo-skill", body=body)
    diagnostics = _check(tmp_path, "demo-skill")
    assert "SKILL_OUTPUT_FORMAT_MISSING" in _codes(diagnostics)
    # A warning, so it reports without blocking; the CLI exits on errors only.
    warning = next(d for d in diagnostics if d.code == "SKILL_OUTPUT_FORMAT_MISSING")
    assert warning.severity is Severity.WARNING


def test_emphasis_budget_exceeded(tmp_path: Path) -> None:
    extra = "You MUST verify. You MUST recheck. NEVER guess. NEVER skip. CRITICAL. CRITICAL.\n"
    _write_skill(tmp_path, "demo-skill", body=_VALID_BODY + extra)
    assert "SKILL_EMPHASIS_BUDGET_EXCEEDED" in _codes(_check(tmp_path, "demo-skill"))


def test_emphasis_budget_allows_exactly_five(tmp_path: Path) -> None:
    extra = "You MUST verify. You MUST recheck. NEVER guess. NEVER skip. CRITICAL step.\n"
    _write_skill(tmp_path, "demo-skill", body=_VALID_BODY + extra)
    assert "SKILL_EMPHASIS_BUDGET_EXCEEDED" not in _codes(_check(tmp_path, "demo-skill"))


def test_missing_referenced_local_file(tmp_path: Path) -> None:
    body = _VALID_BODY + "\nRead `skills/demo-skill/notes.md` before starting.\n"
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_REFERENCED_FILE_MISSING" in _codes(_check(tmp_path, "demo-skill"))


def test_existing_referenced_local_file_with_anchor(tmp_path: Path) -> None:
    body = _VALID_BODY + "\nRead `skills/demo-skill/SKILL.md#steps` before starting.\n"
    _write_skill(tmp_path, "demo-skill", body=body)
    assert "SKILL_REFERENCED_FILE_MISSING" not in _codes(_check(tmp_path, "demo-skill"))


def test_wrappers_missing(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "demo-skill")
    parsed, _ = parse_skill(directory)
    assert parsed is not None
    diagnostics = check_wrappers(parsed, tmp_path)
    assert _codes(diagnostics) == ["SKILL_WRAPPER_MISSING", "SKILL_WRAPPER_MISSING"]


def test_wrapper_drift_after_hand_edit(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo-skill")
    generate_all(tmp_path)
    wrapper = tmp_path / ".claude" / "skills" / "demo-skill" / "SKILL.md"
    wrapper.write_text(wrapper.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    diagnostics = verify_all_skills(tmp_path)
    assert _codes(diagnostics) == ["SKILL_WRAPPER_DRIFT"]
    assert diagnostics[0].item_ref == "demo-skill"


def test_orphan_wrapper(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo-skill")
    generate_all(tmp_path)
    orphan = tmp_path / ".codex" / "skills" / "ghost-skill"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("orphaned\n", encoding="utf-8")
    diagnostics = verify_all_skills(tmp_path)
    assert _codes(diagnostics) == ["SKILL_WRAPPER_DRIFT"]
    assert diagnostics[0].item_ref == "ghost-skill"


def test_fully_valid_skill_passes_cleanly(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "demo-skill")
    generate_all(tmp_path)
    parsed, parse_diagnostics = parse_skill(directory)
    assert parsed is not None
    assert parse_diagnostics == []
    assert check_skill(parsed, tmp_path) == []
    assert check_wrappers(parsed, tmp_path) == []


def test_generate_then_verify_all_green(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo-skill")
    _write_skill(tmp_path, "other-skill")
    written = generate_all(tmp_path)
    assert len(written) == 4
    assert verify_all_skills(tmp_path) == []


def test_wrapper_content_shape(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "demo-skill")
    parsed, _ = parse_skill(directory)
    assert parsed is not None
    content = wrapper_content(parsed, ".claude/skills")
    assert content.startswith("---\n")
    assert "# demo-skill wrapper" in content
    assert "`skills/demo-skill/SKILL.md`" in content
    assert "Do not edit" in content


def test_the_verifier_summary_counts_errors_in_english(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One error is an error. "1 error(s)" is a placeholder, not a sentence."""
    from glite_english_audit.verification import verify_skills as cli

    def _one(_root: Path) -> list[Diagnostic]:
        return [Diagnostic.from_code("SKILL_MISSING_FILE", "no SKILL.md", item_ref="demo")]

    monkeypatch.setattr(cli, "verify_all_skills", _one)
    assert cli.main() == 1
    assert "skill verification failed with 1 error\n" in capsys.readouterr().err

    def _two(_root: Path) -> list[Diagnostic]:
        return _one(_root) * 2

    monkeypatch.setattr(cli, "verify_all_skills", _two)
    assert cli.main() == 1
    assert "skill verification failed with 2 errors\n" in capsys.readouterr().err


def test_the_wrapper_generator_counts_files_in_english(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from glite_english_audit.verification import generate_wrappers as cli

    monkeypatch.setattr(cli, "generate_all", lambda _root: [Path("one")])
    assert cli.main() == 0
    assert capsys.readouterr().out == "generated 1 wrapper file\n"

    monkeypatch.setattr(cli, "generate_all", lambda _root: [Path("one"), Path("two")])
    assert cli.main() == 0
    assert capsys.readouterr().out == "generated 2 wrapper files\n"
