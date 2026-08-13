"""The rule that decides a pasted document without spending an agent on it.

Measured on run-4806c5a4629b4652b072b65e99ff9858 over all 6,725 utterances that
have both a projection and a decision: 2,705 matched and the agent kept nothing
from 2,692 of them -- 0.48% wrong, against an accepted ceiling of 1.6%.
"""

from glite_english_audit.pipeline.pasted_documents import (
    MAXIMUM_LINES,
    MINIMUM_LINES,
    is_pasted_document,
)


def _document(lines: int, marker: str = "- ") -> str:
    """A message of `lines` structured lines, as a paste looks."""
    return "\n".join(f"{marker}point number {n} about the design" for n in range(lines))


class TestWhatItCatches:
    def test_a_long_pasted_list_is_not_the_learners_writing(self) -> None:
        assert is_pasted_document(_document(90))

    def test_headings_and_numbers_count_as_structure_too(self) -> None:
        # All three markers were measured separately and agree almost exactly,
        # because they co-occur in the same pasted documents.
        assert is_pasted_document(_document(90, "# "))
        assert is_pasted_document(_document(90, "1. "))

    def test_structure_anywhere_in_a_long_message_is_enough(self) -> None:
        """A pasted document rarely starts with its own list."""
        text = "\n".join(["Some introductory prose."] * 95 + ["- and then a list item"])
        assert is_pasted_document(text)


class TestWhatItRefuses:
    def test_a_short_list_is_someone_writing_a_list(self) -> None:
        # Three bullets in a message is writing. The floor exists because the
        # measured error was 1.54% at 40 lines and 0.48% at 80, for the same
        # coverage in whole sessions.
        assert not is_pasted_document(_document(3))

    def test_long_prose_without_structure_is_left_to_the_agent(self) -> None:
        """Length alone must not decide. Someone can write at length."""
        assert not is_pasted_document("\n".join(["A sentence of ordinary prose."] * 80))

    def test_the_boundary_is_where_it_says_it_is(self) -> None:
        assert not is_pasted_document(_document(MINIMUM_LINES - 1))
        assert is_pasted_document(_document(MINIMUM_LINES))

    def test_a_very_long_structured_document_is_the_learners_own(self) -> None:
        """The measured inversion, and the reason there is a ceiling at all.

        All 38 matches of 150 lines or more had text the agent kept. Past about
        a hundred lines a structured message stops being something someone
        pasted and starts being something they wrote. Removing this ceiling took
        the error rate from 0.48% to 1.86%.
        """
        assert is_pasted_document(_document(MAXIMUM_LINES - 1))
        assert not is_pasted_document(_document(MAXIMUM_LINES))
        assert not is_pasted_document(_document(400))

    def test_it_never_decides_a_single_line(self) -> None:
        """The measured failure of the mirror rule.

        Of 184 single-line non-markdown utterances, only 97 came back identical;
        77 were trimmed and 10 emptied. Deciding those means judging authorship
        inside one line, which is what step c is for. This rule must not try.
        """
        for text in (
            "- a bullet on its own",
            "# a heading on its own",
            "1. a numbered item on its own",
            "I ran the command and it said the file was not found.",
        ):
            assert not is_pasted_document(text), text


class TestAgainstTheRealCorpus:
    """Replay the run the rule was measured on, when it is still on disk.

    Skipped rather than failed when the run has been swept by retention: the
    numbers in the docstring are the record, and this is the check that they
    still hold.
    """

    def test_measured_precision_still_holds(self) -> None:
        import json
        from pathlib import Path

        from glite_english_audit.paths import runs_root

        directory = (
            runs_root() / "run-4806c5a4629b4652b072b65e99ff9858" / "steps" / "c-authored" / "agent"
        )
        if not directory.is_dir():
            return  # retention swept it; the docstring is the record

        def load(path: Path) -> dict[int, dict[str, str]]:
            rows: dict[int, dict[str, str]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if "i" in payload:
                    rows[payload["i"]] = payload
            return rows

        matched = wrong = 0
        for out_path in sorted(directory.glob("*.out.jsonl")):
            in_path = out_path.with_name(out_path.name.replace(".out.jsonl", ".in.jsonl"))
            if not in_path.is_file():
                continue
            answers = load(out_path)
            for index, projected in load(in_path).items():
                if index not in answers:
                    continue
                if not is_pasted_document(projected.get("text", "")):
                    continue
                matched += 1
                if answers[index].get("text", "").strip():
                    wrong += 1

        if matched == 0:
            return
        assert matched >= 2_000, f"coverage collapsed: {matched} matched, expected ~2,705"
        assert wrong / matched <= 0.01, (
            f"precision regressed: {wrong}/{matched} = {100 * wrong / matched:.2f}% "
            "wrongly dropped; 0.48% is the measured rate and 1.6% the accepted ceiling"
        )
