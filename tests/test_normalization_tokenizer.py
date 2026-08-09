"""Unicode fixture suite for the deterministic word tokenizer (spec 5.6)."""

import unicodedata

from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words, words


def test_version_is_semver() -> None:
    parts = TOKENIZER_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_plain_sentence() -> None:
    assert count_words("The quick brown fox jumps over the lazy dog") == 9


def test_contraction_counts_once() -> None:
    assert words("I don't know") == ["I", "don't", "know"]


def test_curly_apostrophe_contraction_counts_once() -> None:
    assert words("I don’t know") == ["I", "don’t", "know"]


def test_hyphenated_compound_counts_once() -> None:
    assert words("a photo-generator works") == ["a", "photo-generator", "works"]


def test_nfd_input_normalized_to_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "café déjà")
    assert count_words(decomposed) == 2


def test_standalone_numbers_do_not_count() -> None:
    assert count_words("I have 42 apples") == 3


def test_punctuation_and_emoji_do_not_count() -> None:
    assert count_words("Great! 🎉 ... !!!") == 1


def test_urls_do_not_count() -> None:
    assert count_words("see https://example.com/page for details") == 3
    assert count_words("visit www.example.com now") == 2


def test_emails_do_not_count() -> None:
    assert count_words("mail me at someone@example.com today") == 4


def test_paths_do_not_count() -> None:
    assert count_words("open /usr/local/bin/tool please") == 2
    assert count_words("check ~/notes.txt now") == 2
    assert count_words(r"look in C:\Users\demo\file.txt please") == 3


def test_filenames_do_not_count() -> None:
    assert count_words("run main.py again") == 2


def test_code_tokens_do_not_count() -> None:
    assert count_words("call snake_case_name here") == 2
    assert count_words("use camelCaseName here") == 2
    assert count_words("print(value) shows it") == 2
    # Bare letter variables survive the token rules; removing whole code spans
    # is the authorship filter's job, before text reaches the tokenizer.
    assert count_words("x = y + 1") == 2


def test_trailing_punctuation_stripped_before_classification() -> None:
    assert words("It works.") == ["It", "works"]
    assert words("(really)") == ["really"]


def test_quoted_words_count() -> None:
    assert words('say "hello" twice') == ["say", "hello", "twice"]


def test_non_latin_letters_count_as_words() -> None:
    # The tokenizer counts letter runs in any script; English filtering is the
    # language layer's job, not the tokenizer's.
    assert count_words("привет мир") == 2


def test_mixed_sentence() -> None:
    text = "Yesterday I pushed 3 commits to https://github.com/example/repo and don't regret it"
    assert words(text) == [
        "Yesterday",
        "I",
        "pushed",
        "commits",
        "to",
        "and",
        "don't",
        "regret",
        "it",
    ]


def test_empty_and_whitespace() -> None:
    assert count_words("") == 0
    assert count_words("   \n\t  ") == 0


# A fixed paragraph exercising the tricky rules together rather than one at a
# time: contractions, a hyphenated compound, a URL, an email, a source path
# with line and column, a filename, decomposed and precomposed accents, a
# diaeresis, standalone numbers, a version-like token, and a trailing
# semicolon. Real prose combines these; the rule-by-rule tests above do not.
GOLDEN_TEXT = (
    "I very like this plan, but I am agree with Dana that we should not deploy on Friday. "
    "She wrote: it's a well-known problem — see https://example.invalid/docs and "
    "reports@example.invalid for the details. Run npm run build, check src/app.ts:14:3, "
    "then read the naïve café notes (2026-08-09, 42 items, 3.5x faster). "
    "Résumé, coöperate, and re-run; don't forget the 5m folder."
)
GOLDEN_COUNT = 55


def test_the_golden_paragraph_still_counts_the_same() -> None:
    """The word count is the denominator of every rate this product reports.

    The rule-by-rule tests above each pin one behavior, so a rewrite that
    satisfies all of them can still count real prose differently — and every
    reported error rate would move with it, silently, because nothing compares
    a whole paragraph to a known total.

    If this number changes, the tokenizer's behavior changed. That is allowed,
    but it makes every previously reported rate incomparable, so TOKENIZER_VERSION
    must change with it and this constant must be updated deliberately.
    """
    assert count_words(GOLDEN_TEXT) == GOLDEN_COUNT
    assert TOKENIZER_VERSION == "1.0.0", (
        "the tokenizer version changed; update GOLDEN_COUNT deliberately and "
        "record that older runs' rates are not comparable"
    )


def test_the_golden_count_is_not_trivially_reachable() -> None:
    # Guards the vector itself: if count_words became "split on whitespace" it
    # would return a different, larger number, so the constant is evidence of
    # the real rules rather than of any counting at all.
    assert len(GOLDEN_TEXT.split()) > GOLDEN_COUNT
