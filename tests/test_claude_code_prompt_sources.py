"""Which prompts count as the learner's writing, and which are discarded.

Written after a reporter was told he had produced 386 words of English. He had
produced far more; he had typed it into the Claude Code desktop app, which
records ``promptSource: "sdk"``, and the allowlist here recognised only the
three values that a terminal produces. His machine held 1,024 files stamped
``entrypoint: claude-desktop`` and 977 prompts stamped ``sdk``, and the audit
read every one of them, parsed every one of them, and threw every one away
without a word.

No test referenced ``promptSource`` before this file existed, which is how an
allowlist built from one machine reached the people using another.
"""

from typing import Any

import pytest

from glite_english_audit.adapters.claude_code.records import (
    RecordOutcome,
    classify_record,
)

TEXT = "I has been working on this for two days."


def _prompt(**overrides: Any) -> dict[str, Any]:
    """A user record in the shape the CLI writes, before any override."""
    record: dict[str, Any] = {
        "type": "user",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "cwd": "/somewhere",
        "userType": "external",
        "isSidechain": False,
        "entrypoint": "cli",
        "promptSource": "typed",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": TEXT},
    }
    record.update(overrides)
    return record


class TestTheTerminal:
    @pytest.mark.parametrize("source", ["typed", "queued", "suggestion_accepted"])
    def test_a_typed_prompt_is_the_learners_writing(self, source: str) -> None:
        result = classify_record(_prompt(promptSource=source))

        assert result.outcome is RecordOutcome.KEPT
        assert result.text == TEXT


class TestTheDesktopApp:
    """The case that was silently dropped."""

    def test_a_desktop_prompt_is_the_learners_writing(self) -> None:
        """977 of these were discarded on the reporter's machine."""
        result = classify_record(_prompt(promptSource="sdk", entrypoint="claude-desktop"))

        assert result.outcome is RecordOutcome.KEPT, (
            "a person typing into the desktop app wrote this"
        )
        assert result.text == TEXT

    @pytest.mark.parametrize(
        "entrypoint",
        ["cli", "sdk-cli", "claude-desktop", "claude-desktop-3p"],
    )
    def test_every_interactive_client_counts(self, entrypoint: str) -> None:
        """The VS Code extension and the JetBrains plugin are SDK hosts too.

        They spawn the CLI and hand it what the user typed, so their records
        carry the same `sdk` value the desktop app writes.
        """
        result = classify_record(_prompt(promptSource="sdk", entrypoint=entrypoint))

        assert result.outcome is RecordOutcome.KEPT, entrypoint


class TestWhatStaysOut:
    """The allowlist is narrowed here, not removed."""

    def test_an_sdk_prompt_with_no_entrypoint_is_still_excluded(self) -> None:
        """`sdk` alone could be a script driving Claude Code.

        Nobody typed those, and admitting them would put text the learner never
        wrote into a report about the learner's English.
        """
        record = _prompt(promptSource="sdk")
        del record["entrypoint"]

        assert classify_record(record).outcome is not RecordOutcome.KEPT

    def test_an_sdk_prompt_from_an_unfamiliar_client_is_still_excluded(self) -> None:
        result = classify_record(_prompt(promptSource="sdk", entrypoint="some-automation"))

        assert result.outcome is not RecordOutcome.KEPT

    def test_a_system_prompt_is_never_the_learners_writing(self) -> None:
        """It is the product talking to itself, whatever the entrypoint."""
        result = classify_record(
            _prompt(promptSource="system", entrypoint="claude-desktop", isMeta=True)
        )

        assert result.outcome is not RecordOutcome.KEPT

    def test_an_unknown_source_is_excluded_and_flagged(self) -> None:
        """Fail closed, but leave the trace that says why the number is small.

        This flag is the difference between "you wrote very little" and "this
        version does not understand what you wrote", and it is the reason the
        reporter's 386 words could not be diagnosed from the run itself.
        """
        result = classify_record(_prompt(promptSource="something_new", entrypoint="cli"))

        assert result.outcome is not RecordOutcome.KEPT
        assert result.unknown_origin, "an unrecognised source must leave a trace"

    def test_a_non_human_origin_still_overrides_an_interactive_entrypoint(self) -> None:
        """Origin is judged before the prompt source and stays authoritative."""
        result = classify_record(
            _prompt(promptSource="sdk", entrypoint="claude-desktop", origin={"kind": "committed"})
        )

        assert result.outcome is not RecordOutcome.KEPT
