"""Setup is one statement and one question, and stays that way.

A real session asked four times before any work began: may I scan, which of
nine apps, which period, may I send — then confirmed the numbers it had already
consented to act on. Every one of those asked the learner to weigh something
they had no basis to judge.

Two changes collapsed it. An audit reads Claude Code and nothing else, so there
is no app question; and the messages were typed into Claude Code, so analysing
them inside Claude Code discloses them to nobody new, which is why there is no
second question about sending text to a provider.

What is pinned here is the shape, not the wording: the statement of what will be
read comes before the question that asks for it, and the questions the
simplification removed cannot quietly return.
"""

import re

from glite_english_audit.paths import repo_root

_SKILL = repo_root() / "skills" / "run-english-audit" / "SKILL.md"
_NUMBERED_STEP = re.compile(r"^(\d+)\. (.+)$", re.MULTILINE)


def _body() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _steps() -> list[tuple[int, int, str]]:
    return [(m.start(), int(m.group(1)), m.group(2)) for m in _NUMBERED_STEP.finditer(_body())]


def test_the_steps_are_numbered_without_a_gap() -> None:
    """Removing a step is easy to half-finish."""
    numbers = [number for _start, number, _title in _steps()]
    assert numbers == list(range(1, len(numbers) + 1)), f"step numbering broken: {numbers}"


def test_what_will_be_read_is_stated_before_it_is_asked_for() -> None:
    """Consent given against a disclosure that has not happened is not consent."""
    body = _body()
    statement = body.index("I'll read your <runtime> history")
    question = body.index("may I read your <runtime> history")
    assert statement < question


def test_no_second_consent_gate_returns() -> None:
    """The transfer question and the preflight confirmation are both gone.

    Each phrase below marked one of them. They are banned rather than merely
    absent, because both read as prudent additions to anyone who has not
    followed why one recipient means one question.
    """
    body = _body()
    banned = (
        "Consent moment 2",
        "Consent moment 3",
        "last question before processing",
    )
    present = [phrase for phrase in banned if phrase in body]
    assert not present, f"a removed gate came back: {present}"


def test_no_source_selection_question_returns() -> None:
    """An audit reads one source and does not put the choice to the learner."""
    body = _body()
    banned = ("Which apps should I audit", "Which apps should I skip")
    present = [phrase for phrase in banned if phrase in body]
    assert not present, f"the app menu came back: {present}"
