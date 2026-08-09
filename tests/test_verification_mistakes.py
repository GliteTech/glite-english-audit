"""Stage 5 must not count one mistake twice.

Specification 5.6 makes stage-5 records occurrence-based and atomic so that
"verifiers can detect double counting", and no verifier did. The only consumer
of these records took len(mistakes), so the verified-mistake total — the
numerator of the learner's error rate, and the figure every withheld class must
add up to — was whatever the model emitted.

The risk was live. On the measured run the model turned 62 findings into 75
records by splitting blocks that named two errors, and this verifier's first
execution found two records whose spans were fully contained inside another
record's span: the same characters counted twice.
"""

from pathlib import Path

from glite_english_audit.artifacts.enums import Modality
from glite_english_audit.artifacts.models import EvidenceSpan, PrivateMistake
from glite_english_audit.verification.verify_mistakes import verify_mistakes

_TEXT = "I very like this plan and I am agree with the second variant."
_UTTERANCE = "claude_code-0123456789abcdef-u1"
_CORPUS = {_UTTERANCE: _TEXT}


def _mistake(index: int, start: int, end: int, *, occurrence: str | None = None) -> PrivateMistake:
    return PrivateMistake(
        mistake_id=f"mst-{index}",
        occurrence_id=occurrence or f"occ-{index}",
        finding_artifact_id="art-" + "0" * 32,
        utterance_id=_UTTERANCE,
        evidence_span=EvidenceSpan(start=start, end=end),
        original_text=_TEXT[start:end],
        correction="corrected",
        explanation="why",
        modality=Modality.WRITTEN,
        source_adapter="claude_code",
        session_hash="a" * 64,
    )


def _codes(mistakes: list[PrivateMistake]) -> list[str]:
    return [d.code for d in verify_mistakes(mistakes, _CORPUS)]


def test_two_separate_mistakes_in_one_sentence_are_both_kept() -> None:
    # "very like" and "am agree" are two errors, not one. Atomicity forbids
    # counting one twice, not counting two.
    assert _codes([_mistake(1, 2, 11), _mistake(2, 28, 36)]) == []


def test_a_span_inside_another_span_is_a_double_count() -> None:
    # This is the exact shape found on the real run: the model split one
    # finding into two records and the narrower one sat inside the wider.
    assert _codes([_mistake(1, 2, 20), _mistake(2, 7, 16)]) == ["CARDINALITY_MISMATCH"]


def test_partly_overlapping_spans_are_a_double_count() -> None:
    assert _codes([_mistake(1, 2, 16), _mistake(2, 10, 24)]) == ["CARDINALITY_MISMATCH"]


def test_spans_that_merely_touch_are_not_a_double_count() -> None:
    # The span is half-open, so [2,11) and [11,20) share no character.
    assert _codes([_mistake(1, 2, 11), _mistake(2, 11, 20)]) == []


def test_a_repeated_occurrence_id_is_a_double_count() -> None:
    codes = _codes(
        [_mistake(1, 2, 11, occurrence="occ-x"), _mistake(2, 28, 36, occurrence="occ-x")]
    )
    assert codes == ["CARDINALITY_MISMATCH"]


def test_a_span_that_does_not_hold_the_quoted_text_is_refused() -> None:
    # The span is what makes the record checkable. A quote that does not match
    # it could be a paraphrase or an invention, and nothing downstream reads
    # the utterance again to find out.
    bad = _mistake(1, 2, 11).model_copy(update={"original_text": "something else"})
    assert _codes([bad]) == ["SCHEMA_INVALID_VALUE"]


def test_a_span_past_the_end_of_its_utterance_is_refused() -> None:
    far = _mistake(1, 2, 11).model_copy(
        update={"evidence_span": EvidenceSpan(start=2, end=len(_TEXT) + 50)}
    )
    assert _codes([far]) == ["SCHEMA_INVALID_VALUE"]


def test_a_record_citing_an_utterance_outside_the_corpus_is_refused() -> None:
    orphan = _mistake(1, 2, 11).model_copy(
        update={"utterance_id": "claude_code-0123456789abcdef-missing"}
    )
    assert _codes([orphan]) == ["LINEAGE_MISSING_INPUT"]


def test_an_empty_run_passes(tmp_path: Path) -> None:
    # A learner with no high-confidence mistakes is a valid outcome, not a
    # verification failure.
    assert _codes([]) == []
