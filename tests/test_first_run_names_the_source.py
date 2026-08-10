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

from glite_english_audit.paths import repo_root
from glite_english_audit.pipeline.start_run import PRIMARY_ADAPTER

_SKILL = repo_root() / "skills" / "run-english-audit" / "SKILL.md"


def _body() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_the_opening_sentence_names_claude_code() -> None:
    assert "I'll read your Claude Code history" in _body()


def test_the_named_source_is_the_one_the_code_selects() -> None:
    """Prose and selection must name the same source.

    A sentence promising Claude Code above a default that reads nine apps is
    the defect this file exists to prevent, in the direction that matters.
    """
    assert PRIMARY_ADAPTER == "claude_code"


def test_the_opening_does_not_offer_the_other_applications() -> None:
    """They are offered only when Claude Code holds too little for a report.

    Naming them in the first sentence puts a decision in front of someone who
    has no basis to make it, and widens the privacy surface being agreed to.
    """
    opening = _body().split("4. Discovery")[0]
    for other in ("Cursor", "Wispr Flow", "OpenCode", "Aider", "Gemini"):
        assert other not in opening, f"the opening names {other}"


def test_the_sentence_still_says_what_leaves_the_machine() -> None:
    """Naming the source must not crowd out the limit on where it goes."""
    # The skill wraps its prose, so compare on collapsed whitespace.
    body = " ".join(_body().lower().split())
    assert "nothing goes to glite except the list of mistakes" in body
    assert "you see it first" in body


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
