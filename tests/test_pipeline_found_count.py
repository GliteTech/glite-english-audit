"""The evidence-based stop: counting what step d found, and deriving what it read.

The audit stops on evidence, not on a calendar: step d works newest first and
halts once it has found enough mistakes, leaving the oldest sessions
unanalyzed. Two things make that honest rather than silent, and both are
tested here from the files alone -- no agent's claim is trusted:

- ``found_count`` says when enough is enough, by counting step d's own output;
- ``analyzed_ids_from_steps`` derives which utterances were actually read, so
  the review's analyzed denominator shrinks instead of pretending the tail
  was error-free.
"""

from pathlib import Path

from glite_english_audit.artifacts.enums import Modality, StepId, TextStatus
from glite_english_audit.artifacts.io import ensure_private_dir
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline.build_review import analyzed_ids_from_steps
from glite_english_audit.pipeline.found_count import TARGET_FOUND_MISTAKES, count_found

_RUN = "run-0123456789abcdef0123456789abcdef"


def _utterance(number: int, text: str) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"u-{number:04d}",
        source_adapter="claude_code",
        adapter_version="1",
        session_hash="s" * 16,
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=1.0,
        authorship_basis="test fixture",
        source_path_hash="h" * 16,
    )


def _write_session(directory: Path, name: str, utterances: list[NormalizedUtterance]) -> None:
    ensure_private_dir(directory)
    lines = "".join(u.model_dump_json() + "\n" for u in utterances)
    (directory / name).write_text(lines, encoding="utf-8")


def test_found_count_reads_lines_and_says_when_enough(tmp_path: Path) -> None:
    d_dir = step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path)
    ensure_private_dir(d_dir / "agent")
    (d_dir / "agent" / "session-0002.out.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    (d_dir / "agent" / "session-0003.out.jsonl").write_text('{"a":3}\n\n', encoding="utf-8")

    report = count_found(_RUN, runs_root=tmp_path)

    assert report["found"] == 3
    assert report["sessions_analyzed"] == 2
    assert report["enough"] is False

    # One line is one record, so the threshold is exact, not approximate.
    (d_dir / "agent" / "session-0004.out.jsonl").write_text(
        '{"a":0}\n' * (TARGET_FOUND_MISTAKES - 3), encoding="utf-8"
    )
    assert count_found(_RUN, runs_root=tmp_path)["enough"] is True


def test_the_target_lands_in_the_report_band() -> None:
    # 240 found, less the roughly one-seventh verification drops, lands at the
    # bottom of the 200-300 verified band the report is designed for.
    assert 200 <= TARGET_FOUND_MISTAKES * 6 // 7 <= 300


def test_analyzed_ids_come_from_what_step_d_actually_read(tmp_path: Path) -> None:
    c_dir = step_dir(_RUN, StepId.C_AUTHORED, root=tmp_path)
    _write_session(c_dir, "session-0001.jsonl", [_utterance(1, "Oldest text.")])
    _write_session(
        c_dir,
        "session-0002.jsonl",
        [_utterance(2, "Newest text."), _utterance(3, "")],
    )
    d_dir = step_dir(_RUN, StepId.D_MISTAKES, root=tmp_path)
    ensure_private_dir(d_dir)
    # Step d read only the newest session before the stop rule fired.
    (d_dir / "session-0002.jsonl").write_text("", encoding="utf-8")

    analyzed = analyzed_ids_from_steps(_RUN, runs_root=tmp_path)

    # The read session's real text counts; its emptied item does not, and the
    # never-read session contributes nothing -- it stays eligible, unanalyzed.
    assert analyzed == {"u-0002"}
