"""What counts as an English word, and why the artifact and the count differ.

The nine-stage pipeline ran ``classify_english`` and rewrote each utterance to
its English slice. The five-step pipeline keeps step c's text verbatim — it is
diffed against step b line by line — so the language rule had nowhere left to
live and briefly had no caller at all. That is the shape of every defect this
project keeps finding: a capability that still exists, still passes its own
tests, and is called by nothing.

It now lives in the word count. The file says what the learner wrote; the count
says how much of it was English. Both are true, and this file is what keeps them
that way.
"""

from pathlib import Path

from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.pipeline.authorship import english_words


def test_english_text_counts_every_word() -> None:
    assert english_words("please retry the deployment now") == 5


def test_a_sentence_in_another_language_counts_for_nothing() -> None:
    # The learner did write it, and it stays in the step-c file. It is not
    # English they got wrong, so it is not part of the denominator of an
    # English error rate.
    russian = "нужно починить деплой скрипт сегодня"
    assert count_words(russian) > 0
    assert english_words(russian) == 0


def test_a_foreign_word_inside_an_english_sentence_is_not_counted() -> None:
    mixed = "please retry the deployment сегодня now again today"
    assert english_words(mixed) == count_words(mixed) - 1


def test_empty_text_counts_for_nothing() -> None:
    # Step c writes empty text for an utterance that was entirely someone
    # else's, so this is the normal case, not an edge one.
    assert english_words("") == 0


def test_the_count_never_exceeds_the_plain_word_count() -> None:
    # The English slice is a subset of the text by construction. If this ever
    # inverts, the denominator has grown words nobody wrote.
    for text in (
        "just a normal English sentence",
        "нужно починить деплой",
        "we go now жим",
        "The café menu is déjà outdated",
        "",
        "12345 --- !!! 6789",
    ):
        assert english_words(text) <= count_words(text), text


def test_the_verifier_counts_words_the_same_way_the_step_does(tmp_path: Path) -> None:
    """Both sides of the check must agree on what a word is.

    The verifier recounted with ``count_words`` while step c recorded
    ``english_words``, so every run that kept any non-English text failed
    deterministic verification with ARITHMETIC_INVARIANT_VIOLATION — the check
    disagreeing with the step, and blaming the step. Nothing caught it because
    every fixture corpus was pure English.
    """
    import inspect

    from glite_english_audit.verification import verify_corpus as verifier

    source = inspect.getsource(verifier.verify_corpus)
    assert "english_words(" in source
    assert "count_words(" not in source
