"""``/audit`` is an entry point, and it must keep pointing at a procedure that exists.

The command is deliberately not a canonical skill. Every skill under ``skills/``
writes an artifact, which is why ``verify_skills`` demands ``## Output Format``
of all of them unconditionally; a launcher writes nothing, so making it a skill
would mean either a section that describes an artifact it never produces or a
verifier loosened for one file. It is a slash command instead, and it says so.

What a hand-written entry point can do that a generated wrapper cannot is rot.
Rename or move the skill and ``/audit`` still resolves, still looks right, and
sends the user nowhere. This file is the check that would have caught that.
"""

import re

from glite_english_audit.paths import repo_root

_COMMAND = repo_root() / ".claude" / "commands" / "audit.md"

# Any repo-relative path the command tells the agent to follow.
_SKILL_REFERENCE = re.compile(r"`(skills/[A-Za-z0-9._/-]+\.md)`")


def _body() -> str:
    assert _COMMAND.is_file(), "/audit lost its command file"
    return _COMMAND.read_text(encoding="utf-8")


def test_the_command_exists_and_describes_itself() -> None:
    """The description is what a user reads in the slash-command list."""
    body = _body()
    assert body.startswith("---"), "the command needs frontmatter to carry a description"
    assert "description:" in body.split("---")[1]


def test_every_skill_it_names_is_a_file_that_exists() -> None:
    """The whole failure mode: a command pointing at a procedure that moved."""
    referenced = _SKILL_REFERENCE.findall(_body())
    assert referenced, "the command names no skill to follow"
    missing = [path for path in referenced if not (repo_root() / path).is_file()]
    assert not missing, f"/audit points at files that do not exist: {missing}"


def test_it_points_at_the_canonical_skill_not_a_generated_wrapper() -> None:
    """Wrappers are generated and say only "read the canonical file".

    Sending the agent to one adds a hop that regeneration can rewrite, and the
    repo already forbids hand-editing them. The command names the source.
    """
    body = _body()
    assert "skills/run-english-audit/SKILL.md" in body
    for wrapper_dir in (".claude/skills", ".codex/skills"):
        assert wrapper_dir not in body, f"/audit must not route through {wrapper_dir}"


def test_the_command_does_not_restate_the_procedure() -> None:
    """A launcher that lists the steps will drift from them, and did.

    The first version described "the two consent moments, discovery, selection,
    preflight, the five steps a-e". Within a day the flow had one setup consent,
    no selection question and no preflight step, and every one of those words was
    false -- while the existing guard still passed, because it only checks that
    the path it points at resolves.

    Re-syncing the list would buy one day. Not having a list is the fix: the
    skill is the only description of the skill, and the command says where it is.
    """
    body = _body().lower()
    # Names of things the procedure has had, or may have again. A launcher that
    # mentions any of them is enumerating a flow it does not own.
    procedural = (
        "consent moment",
        "preflight",
        "selection",
        "discovery",
        "step a",
        "steps a-e",
        "steps a–e",
        "review page",
        "resume check",
    )
    named = [word for word in procedural if word in body]
    assert not named, (
        f"the command restates the procedure: {named}. "
        "Say what it starts and where the procedure lives; do not list it."
    )


def test_the_command_stays_short() -> None:
    """Length is how the enumeration got in. Sixteen lines is generous for a launcher."""
    assert len(_body().splitlines()) <= 16
