"""The one sentence that says what will be read, and the promise inside it.

This file used to check that the opening sentence named four applications and
counted five more, and that the count matched the adapter registry. An audit
now reads Claude Code and nothing else, so there is no count to keep honest --
there is a single named source, which is a stronger thing to promise and a
simpler thing to check.

What must not drift: the sentence names Claude Code, it does not name the other
applications, and it still says the limit that makes it safe to agree to.
"""

import re

from glite_english_audit.artifacts.enums import AgentRuntime
from glite_english_audit.paths import repo_root
from glite_english_audit.pipeline.start_run import PRIMARY_ADAPTERS

_SKILL = repo_root() / "skills" / "run-english-audit" / "SKILL.md"


def _body() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_the_opening_sentence_names_the_active_runtime() -> None:
    """It used to hardcode Claude Code, which was wrong under Codex.

    The skill has always carried a runtime naming rule -- "name only the active
    runtime in every user-facing sentence" -- and every sentence below it named
    one runtime regardless. The placeholder is what makes the rule reachable.
    """
    assert "I'll read your <runtime> history" in _body()


def test_the_placeholder_is_explained_before_it_is_used() -> None:
    """An unexplained `<runtime>` is a sentence that ships with a hole in it."""
    body = _body()
    assert body.index("`<runtime>` below is a placeholder") < body.index(
        "I'll read your <runtime> history"
    )


def test_the_skill_says_how_to_obtain_the_runtime() -> None:
    """The runtime was an Input that nothing told the agent how to acquire.

    That hole is why the naming rule was ignored: an agent that cannot learn
    the runtime can only hardcode one.
    """
    assert "Take it from the wrapper that loaded you" in _body()


def test_no_user_facing_sentence_hardcodes_one_runtime() -> None:
    """The regression this refactor is one careless paste away from.

    Only the naming rule's own Do/Don't examples, an opaque-label sample and the
    worked example may name a runtime literally -- examples have to be concrete,
    and a worked example runs exactly one runtime.
    """
    allowed_markers = (
        'Claude Code say "Claude Code"',
        "(running in Claude Code)",
        "Claude Code or Codex.",
        '--exclude-label "Claude Code 4"',
    )
    example = _body().split("## End-to-End Example")[0]
    for line in example.splitlines():
        if "Claude Code" not in line:
            continue
        assert any(marker in line for marker in allowed_markers), line


def test_the_named_source_is_the_one_the_code_selects() -> None:
    """Prose and selection must name the same source.

    A sentence promising Claude Code above a default that reads nine apps is
    the defect this file exists to prevent, in the direction that matters.
    """
    assert PRIMARY_ADAPTERS[AgentRuntime.CLAUDE_CODE] == "claude_code"
    assert PRIMARY_ADAPTERS[AgentRuntime.CODEX] == "codex"


def test_the_opening_does_not_offer_the_other_applications() -> None:
    """They are offered only when Claude Code holds too little for a report.

    Naming them in the first sentence puts a decision in front of someone who
    has no basis to make it, and widens the privacy surface being agreed to.
    """
    opening = _body().split("4. Discovery")[0]
    for other in ("Cursor", "Wispr Flow", "OpenCode", "Aider", "Gemini"):
        assert other not in opening, f"the opening names {other}"


def test_the_sentence_says_why_reading_it_back_is_not_a_new_disclosure() -> None:
    """The consent moment must answer the question being asked at it.

    It used to promise "nothing goes to Glite except the list of mistakes, and
    you see it first" -- true, and about a decision three steps away. What is
    being asked here is whether the audit may read the history at all, and the
    answer that matters is that the reader already has it. Glite is not a party
    to this step; it gets its own question at the review page, against a list
    the user can see.
    """
    # The skill wraps its prose, so compare on collapsed whitespace.
    body = " ".join(_body().lower().split())
    assert "you already typed into <runtime>" in body
    assert "does not show them to anyone new" in body


def test_the_opening_does_not_narrate_the_resume_check() -> None:
    """On a first run it finds nothing, and saying so is the only trace of it.

    A sentence about looking, followed by a sentence about not finding, spent
    the opening of the product on an errand that concerned nobody.
    """
    body = " ".join(_body().lower().split())
    assert "first, let me check whether you have an unfinished audit" not in body
    assert "no unfinished audit to continue" not in body


def test_the_consent_line_does_not_overstate_the_privacy() -> None:
    """ "Nothing is copied anywhere" was false, and the file refuted it twice.

    The selected text is read by the provider behind this session -- which is
    precisely what makes the Claude Code-only argument work, since that provider
    already received these messages when they were typed. The honest claim is
    narrower and stronger, and privacy text is the one category the styleguide
    says must stay literal.
    """
    # Only what the agent says aloud. The rationale beside it has to name the
    # banned phrase in order to forbid it, which is not the same as claiming it.
    spoken = " ".join(re.findall(r"```text\n(.*?)```", _body(), re.S)).lower()
    assert spoken, "the skill has no spoken blocks to check"
    assert "nothing is copied anywhere" not in spoken
