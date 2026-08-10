"""Nobody agrees to a transfer before reading what it covers.

A real session asked the user to confirm sending their writing to a provider,
*then* showed the preflight -- the volume, the model this session runs, the
hours it would take -- and then asked a third time to confirm the numbers they
had already agreed to act on. The consent was given against a disclosure that
had not happened yet.

The order is the property, not the wording, so that is what this pins: the
preflight step is written before the provider-transfer step, and the transfer
step says it comes after. Prose can be rewritten freely above and below.
"""

import re

from glite_english_audit.paths import repo_root

_SKILL = repo_root() / "skills" / "run-english-audit" / "SKILL.md"

_NUMBERED_STEP = re.compile(r"^(\d+)\. (.+)$", re.MULTILINE)


def _steps() -> list[tuple[int, int, str]]:
    """(position in file, step number, title) for each top-level step."""
    body = _SKILL.read_text(encoding="utf-8")
    return [
        (match.start(), int(match.group(1)), match.group(2))
        for match in _NUMBERED_STEP.finditer(body)
    ]


def _position_of(fragment: str) -> int:
    for start, _number, title in _steps():
        if fragment.lower() in title.lower():
            return start
    raise AssertionError(f"no top-level step titled like {fragment!r}")


def test_the_preflight_is_written_before_the_transfer_consent() -> None:
    """The disclosure has to precede the decision it exists to inform."""
    assert _position_of("Preflight") < _position_of("provider transfer")


def test_the_steps_are_numbered_without_a_gap() -> None:
    """Renumbering after removing a step is easy to half-finish."""
    numbers = [number for _start, number, _title in _steps()]
    assert numbers == list(range(1, len(numbers) + 1)), f"step numbering broken: {numbers}"


def test_no_third_confirmation_returns_after_the_transfer_consent() -> None:
    """The run starts on the transfer answer; anything after it is not a gate.

    "This is the last question before processing" was the tell: a sentence that
    only makes sense when the product has already asked more questions than the
    decision required.
    """
    body = _SKILL.read_text(encoding="utf-8")
    after_consent = body[_position_of("provider transfer") :]
    banned = ("last question before processing", "Consent moment 3")
    present = [phrase for phrase in banned if phrase in after_consent]
    assert not present, f"a third confirmation came back: {present}"
