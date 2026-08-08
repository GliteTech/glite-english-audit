"""Conservative deterministic English-span handling (specification, 2.3, 5.6).

Mixed-language input joins the analysis denominator only when it is
confidently separable; everything else is quarantined. There is no
probabilistic language detection — the rules are exact and identical on
every platform:

1. Text with no letters at all is quarantined (``no_letters``).
2. Pure Latin-script text is accepted as-is when it contains at least one
   ASCII letter (``latin_script``); pure Latin text without a single ASCII
   letter is quarantined (``no_ascii_letters``) because English always
   contains ASCII letters.
3. Text containing non-Latin letters is quarantined whole when non-Latin
   letters exceed ``NON_LATIN_LETTER_PERCENT_LIMIT`` percent of all letters
   (``non_latin_share_exceeded``). The 30% limit is deliberately low: above
   it, the text is plausibly written *in* the other language with embedded
   English terms, and stripping would fabricate an English utterance the
   user never produced.
4. Below the limit, non-Latin material must be cleanly separable: every
   whitespace-delimited token is either fully Latin/neutral or fully
   non-Latin. A single token mixing scripts means the boundary is unclear,
   so the whole text is quarantined (``token_mixes_scripts``).
5. The Latin remainder after stripping non-Latin tokens is accepted
   (``non_latin_tokens_stripped``) only when it counts at least
   ``MIN_REMAINDER_WORDS`` words; shorter remainders are quarantined
   (``remainder_below_minimum``) because a couple of stray Latin words
   inside foreign text are usually names or borrowed terms, not authored
   English.

A character is Latin when its Unicode name starts with ``LATIN``. Fullwidth
Latin forms and other exotic presentation variants are deliberately treated
as non-Latin: when in doubt, quarantine.
"""

import unicodedata
from functools import cache

from pydantic import BaseModel, ConfigDict, model_validator

from glite_english_audit.normalization import tokenizer

PRODUCER_VERSION = "1.0.0"

# Quarantine when non-Latin letters exceed this percentage of all letters.
# Compared with exact integer arithmetic; exactly 30% is still separable.
NON_LATIN_LETTER_PERCENT_LIMIT = 30

# A stripped remainder shorter than this many words is quarantined.
MIN_REMAINDER_WORDS = 3

REASON_LATIN_SCRIPT = "latin_script"
REASON_NO_LETTERS = "no_letters"
REASON_NO_ASCII_LETTERS = "no_ascii_letters"
REASON_NON_LATIN_SHARE_EXCEEDED = "non_latin_share_exceeded"
REASON_TOKEN_MIXES_SCRIPTS = "token_mixes_scripts"
REASON_REMAINDER_BELOW_MINIMUM = "remainder_below_minimum"
REASON_NON_LATIN_TOKENS_STRIPPED = "non_latin_tokens_stripped"


class LanguageDecision(BaseModel):
    """Outcome of English-span classification for one text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    english_text: str | None
    quarantined: bool
    reason: str

    @model_validator(mode="after")
    def _consistent(self) -> "LanguageDecision":
        if self.quarantined and self.english_text is not None:
            msg = "a quarantined decision must not carry english_text"
            raise ValueError(msg)
        if not self.quarantined and self.english_text is None:
            msg = "an accepted decision must carry english_text"
            raise ValueError(msg)
        return self


@cache
def _is_latin_letter(char: str) -> bool:
    return unicodedata.name(char, "").startswith("LATIN")


def _quarantined(reason: str) -> LanguageDecision:
    return LanguageDecision(english_text=None, quarantined=True, reason=reason)


def _accepted(english_text: str, reason: str) -> LanguageDecision:
    return LanguageDecision(english_text=english_text, quarantined=False, reason=reason)


def _has_ascii_letter(text: str) -> bool:
    return any(ch.isascii() and ch.isalpha() for ch in text)


def classify_english(text: str) -> LanguageDecision:
    """Classify ``text`` as countable English, a strippable mix, or quarantine.

    Combining marks are not letters, so decomposed accents do not affect the
    letter counts; the tokenizer applies NFC before counting words.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return _quarantined(REASON_NO_LETTERS)

    non_latin_count = sum(1 for ch in letters if not _is_latin_letter(ch))
    if non_latin_count == 0:
        if not _has_ascii_letter(text):
            return _quarantined(REASON_NO_ASCII_LETTERS)
        return _accepted(text, REASON_LATIN_SCRIPT)

    # Exact integer comparison: quarantine only when strictly above the limit.
    if non_latin_count * 100 > len(letters) * NON_LATIN_LETTER_PERCENT_LIMIT:
        return _quarantined(REASON_NON_LATIN_SHARE_EXCEEDED)

    kept_tokens: list[str] = []
    for token in text.split():
        token_letters = [ch for ch in token if ch.isalpha()]
        has_non_latin = any(not _is_latin_letter(ch) for ch in token_letters)
        if has_non_latin:
            if any(_is_latin_letter(ch) for ch in token_letters):
                return _quarantined(REASON_TOKEN_MIXES_SCRIPTS)
            continue
        kept_tokens.append(token)

    remainder = " ".join(kept_tokens)
    if tokenizer.count_words(remainder) < MIN_REMAINDER_WORDS:
        return _quarantined(REASON_REMAINDER_BELOW_MINIMUM)
    if not _has_ascii_letter(remainder):
        return _quarantined(REASON_NO_ASCII_LETTERS)
    return _accepted(remainder, REASON_NON_LATIN_TOKENS_STRIPPED)
