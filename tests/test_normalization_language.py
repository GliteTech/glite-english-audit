"""Conservative English-span classification (specification, 2.3, 5.6)."""

import pytest
from pydantic import ValidationError

from glite_english_audit.normalization.language import (
    MIN_REMAINDER_WORDS,
    NON_LATIN_LETTER_PERCENT_LIMIT,
    PRODUCER_VERSION,
    REASON_LATIN_SCRIPT,
    REASON_NO_ASCII_LETTERS,
    REASON_NO_LETTERS,
    REASON_NON_LATIN_SHARE_EXCEEDED,
    REASON_NON_LATIN_TOKENS_STRIPPED,
    REASON_REMAINDER_BELOW_MINIMUM,
    REASON_TOKEN_MIXES_SCRIPTS,
    LanguageDecision,
    classify_english,
)


def test_producer_version_is_semver() -> None:
    parts = PRODUCER_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_documented_thresholds() -> None:
    assert NON_LATIN_LETTER_PERCENT_LIMIT == 30
    assert MIN_REMAINDER_WORDS == 3


def test_pure_english_accepted_as_is() -> None:
    text = "Please fix the deploy script, it fails on Windows."
    decision = classify_english(text)
    assert not decision.quarantined
    assert decision.english_text == text
    assert decision.reason == REASON_LATIN_SCRIPT


def test_latin_with_accents_accepted() -> None:
    decision = classify_english("The café menu is déjà outdated")
    assert not decision.quarantined
    assert decision.reason == REASON_LATIN_SCRIPT


def test_empty_text_quarantined() -> None:
    decision = classify_english("")
    assert decision.quarantined
    assert decision.english_text is None
    assert decision.reason == REASON_NO_LETTERS


def test_numbers_and_punctuation_quarantined() -> None:
    decision = classify_english("12345 --- !!! 6789")
    assert decision.quarantined
    assert decision.reason == REASON_NO_LETTERS


def test_pure_latin_without_ascii_letter_quarantined() -> None:
    decision = classify_english("Àé Îø")
    assert decision.quarantined
    assert decision.reason == REASON_NO_ASCII_LETTERS


def test_pure_cyrillic_quarantined() -> None:
    decision = classify_english("нужно починить деплой скрипт")
    assert decision.quarantined
    assert decision.reason == REASON_NON_LATIN_SHARE_EXCEEDED


def test_exactly_thirty_percent_non_latin_is_separable() -> None:
    # Letters: "we go now" = 7 Latin, Cyrillic token = 3 -> exactly 30%.
    decision = classify_english("we go now жим")
    assert not decision.quarantined
    assert decision.english_text == "we go now"
    assert decision.reason == REASON_NON_LATIN_TOKENS_STRIPPED


def test_just_above_thirty_percent_quarantined() -> None:
    # Letters: 7 Latin, 4 Cyrillic -> ~36% > 30%.
    decision = classify_english("we go now жизн")
    assert decision.quarantined
    assert decision.reason == REASON_NON_LATIN_SHARE_EXCEEDED


def test_mixed_script_token_quarantined() -> None:
    # "жиok" mixes Cyrillic and Latin inside one token: not separable.
    decision = classify_english("things look mostly fine жиok today")
    assert decision.quarantined
    assert decision.reason == REASON_TOKEN_MIXES_SCRIPTS


def test_cjk_tokens_stripped_when_separable() -> None:
    decision = classify_english("你好 need to ship this build today")
    assert not decision.quarantined
    assert decision.english_text == "need to ship this build today"
    assert decision.reason == REASON_NON_LATIN_TOKENS_STRIPPED


def test_cjk_glued_to_english_quarantined() -> None:
    decision = classify_english("你好need to ship this build today")
    assert decision.quarantined
    assert decision.reason == REASON_TOKEN_MIXES_SCRIPTS


def test_short_remainder_quarantined() -> None:
    # 23% non-Latin letters, separable, but only one English word remains.
    decision = classify_english("understand жим")
    assert decision.quarantined
    assert decision.reason == REASON_REMAINDER_BELOW_MINIMUM


def test_three_word_remainder_is_the_minimum() -> None:
    decision = classify_english("please retry deployment жим")
    assert not decision.quarantined
    assert decision.english_text == "please retry deployment"


def test_neutral_tokens_survive_stripping() -> None:
    decision = classify_english("retry build 42 now жим")
    assert not decision.quarantined
    assert decision.english_text == "retry build 42 now"


def test_decision_model_rejects_inconsistent_states() -> None:
    with pytest.raises(ValidationError):
        LanguageDecision(english_text="text", quarantined=True, reason="x")
    with pytest.raises(ValidationError):
        LanguageDecision(english_text=None, quarantined=False, reason="x")


def test_deterministic() -> None:
    text = "mostly English text плюс немного слов"
    first = classify_english(text)
    second = classify_english(text)
    assert first == second
