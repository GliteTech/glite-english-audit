"""Every command a skill tells an agent to run must exist and accept its flags.

Skills are prose, and prose drifts from code silently. Three separate defects
this project has already shipped were of exactly this kind: a skill naming a
verifier module that had moved, a skill whose step-3 commands were missing so
step 4 had nothing to read, and a skill passing a flag that had been renamed.
None of them failed a test, because nothing compared the two.

An agent following a skill cannot recover from a command that does not exist.
It reports the failure, and the user's run stops at a step that was supposed to
be automatic — so this is worth a test even though it only checks that things
are callable.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from glite_english_audit.paths import repo_root

_SKILLS = repo_root() / "skills"

# `uv run python -m glite_english_audit.<module>` as it appears in a skill,
# tolerating the line breaks the 100-column prose wraps at.
_INVOCATION = re.compile(r"python -m glite_english_audit\.([a-z0-9_.]+)")

# A long flag on the same command, e.g. --run-id or --repair-only.
_FLAG = re.compile(r"--[a-z][a-z0-9-]+")


def _skill_files() -> list[Path]:
    return sorted(_SKILLS.glob("*/SKILL.md"))


def _invocations() -> list[tuple[str, str, str]]:
    """Every (skill, module, command text) a skill asks an agent to run."""
    found: list[tuple[str, str, str]] = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for match in _INVOCATION.finditer(text):
            module = match.group(1).rstrip(".")
            # The command's flags run to the end of the fenced or indented
            # block; a blank line or a sentence in prose ends it.
            tail = text[match.end() : match.end() + 400]
            command = tail.split("\n\n")[0]
            found.append((path.parent.name, module, command))
    return found


def _help(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", f"glite_english_audit.{module}", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root(),
        check=False,
    )


def test_the_skills_name_some_commands() -> None:
    # Guards the regex itself: if it stopped matching, every test below would
    # pass by finding nothing, which is the vacuous shape worth ruling out.
    assert len(_invocations()) >= 8


@pytest.mark.parametrize(
    ("skill", "module"),
    sorted({(skill, module) for skill, module, _ in _invocations()}),
)
def test_every_command_a_skill_names_can_be_run(skill: str, module: str) -> None:
    result = _help(module)
    assert result.returncode == 0, (
        f"{skill} tells the agent to run {module}, which fails: {result.stderr[-400:]}"
    )


@pytest.mark.parametrize(
    ("skill", "module", "command"),
    sorted(set(_invocations())),
)
def test_every_flag_a_skill_passes_is_accepted(skill: str, module: str, command: str) -> None:
    accepted = _help(module).stdout
    if not accepted:
        pytest.skip(f"{module} printed no help to check flags against")
    for flag in sorted(set(_FLAG.findall(command))):
        assert flag in accepted, f"{skill} passes {flag} to {module}, which does not accept it"
