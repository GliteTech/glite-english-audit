"""A command nothing invokes is a feature that does not exist.

This is the defect this project keeps producing. Each time, a capability was
built, tested, and left with no caller: the state machine no driver used, the
privacy report nothing read, the consent fields nothing wrote, the choice-saving
command no skill ran, the stage-3 corpus verifier nobody invoked. Every one had
passing unit tests. Tests prove a function works; they cannot notice that the
product never calls it.

So the check is structural. Every module with a command-line entry point is
either named in a SKILL.md — meaning an agent runs it during a real audit — or
listed below with the reason it has no caller. Adding a command and neither
wiring it nor listing it fails here.

The list is deliberately awkward to extend. Adding a name to it should feel like
a decision, because every previous entry in this class of bug would have looked
reasonable at the time.
"""

import re

import pytest

from glite_english_audit.paths import repo_root

# Commands that legitimately have no skill caller, each with the reason.
# A maintainer tool is run by a person or by CI, never during an audit.
NO_SKILL_CALLER: dict[str, str] = {
    "artifacts.schema_export": (
        "maintainer and CI tool: exports and checks the JSON schemas. Part of the "
        "quality gate, never part of a run."
    ),
    "verification.generate_wrappers": (
        "maintainer tool: regenerates the .claude and .codex skill wrappers. A run "
        "reads the wrappers; it never rebuilds them."
    ),
    "normalization.filter_corpus": (
        "the stage-3 fallback path, applying the pre-filter alone with no model "
        "judgment. Used by tests and where no model is available. A real run uses "
        "the model path, so no skill names it, and the fallback is documented as "
        "understating every rate."
    ),
}


def _skill_text() -> str:
    return " ".join(
        path.read_text(encoding="utf-8") for path in (repo_root() / "skills").glob("*/SKILL.md")
    )


def _modules_named_by_skills() -> set[str]:
    found = re.findall(r"python -m glite_english_audit\.([a-z0-9_.]+)", _skill_text())
    return {name.rstrip(".") for name in found}


def _modules_with_a_command_line() -> set[str]:
    modules: set[str] = set()
    source = repo_root() / "src" / "glite_english_audit"
    for path in source.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if 'if __name__ == "__main__"' not in text or "def main(" not in text:
            continue
        dotted = str(path.relative_to(source)).removesuffix(".py").replace("/", ".")
        modules.add(dotted.removesuffix(".__main__"))
    return modules


def test_the_scan_finds_the_commands_it_is_meant_to_check() -> None:
    # Guards the two extractors. If either stopped matching, every assertion
    # below would pass by comparing empty sets.
    assert len(_modules_with_a_command_line()) >= 10
    assert len(_modules_named_by_skills()) >= 8


@pytest.mark.parametrize("module", sorted(_modules_with_a_command_line()))
def test_every_command_is_run_by_a_skill_or_listed_as_having_no_caller(module: str) -> None:
    if module in NO_SKILL_CALLER:
        assert NO_SKILL_CALLER[module].strip(), f"{module} needs a reason, not an empty string"
        return
    assert module in _modules_named_by_skills(), (
        f"no SKILL.md runs {module}, so nothing in a real audit invokes it. Either name it in "
        f"the skill that should run it, or add it to NO_SKILL_CALLER with the reason it has "
        f"none. Every instance of this in this project so far has been a bug: the capability "
        f"was built and tested, and the product never called it."
    )


def test_the_exemption_list_names_only_real_modules() -> None:
    # An exemption for a module that no longer exists is a stale excuse that
    # would silently cover a future module of the same name.
    unknown = sorted(set(NO_SKILL_CALLER) - _modules_with_a_command_line())
    assert unknown == [], f"NO_SKILL_CALLER names modules with no command line: {unknown}"
