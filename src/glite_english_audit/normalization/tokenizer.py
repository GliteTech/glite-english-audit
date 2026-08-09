"""Deterministic English word counting (specification, 5.6).

The count is identical on every platform: normalize to Unicode NFC, split on
whitespace, exclude non-word tokens whole, then count letter runs with
optional internal straight or curly apostrophes and hyphens. ``don't`` and
``photo-generator`` count as one word each. Standalone numbers, punctuation,
emoji, URLs, email addresses, paths, and code tokens count zero.

Exclusion is deliberately conservative and token-based. A token is excluded
whole when it looks like a URL, email, path, filename, or code identifier;
otherwise its letter runs are counted. Some legitimate words are lost at the
margins (for example ``iPhone`` looks like camelCase); the same rule applies
everywhere, so the denominator stays comparable across sources and platforms.

Any change to these rules must bump ``TOKENIZER_VERSION``; the version is
recorded in every run manifest and partitions calibration history.
"""

import re
import unicodedata

TOKENIZER_VERSION = "1.0.0"

# A word: Unicode letter runs joined by single internal apostrophes or hyphens.
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")

# The exclusion rules above, in one pattern. Exclusion is a boolean OR, so one
# alternation decides it in a single call instead of six. Tokens come from
# ``str.split()`` and so contain no whitespace: ``^`` and ``$`` are the ends of
# the token, which is what makes the bare-filename branch a ``fullmatch``.
_EXCLUDED_TOKEN = re.compile(
    r"://"  # URL scheme
    r"|^[Ww][Ww][Ww]\."  # bare www host, case-insensitively
    r"|@[\w-]+\."  # email address
    r"|^(?:/|~|\.{1,2}/|[A-Za-z]:[\\/])"  # leading path
    r"|[\\/].*[\\/]"  # embedded path
    r"|^[\w-]+\.[A-Za-z]\w{0,3}$"  # bare filename
    r"|[a-z][A-Z][a-z]"  # camelCase identifier
    r"|[_{}()\[\];=<>`$#|&*%^]"  # code punctuation
)

# The only rule above an all-letter token can trip: every other one needs a
# character that is not a letter.
_CAMEL_CASE_TOKEN = re.compile(r"[a-z][A-Z][a-z]")

# Punctuation commonly attached to a word in prose; stripped before the token
# is classified so "time." or "(really)" is judged as its inner word.
_STRIP_CHARS = "\"'“”‘’.,!?;:()[]{}<>…«»—–-*`~"


def words(text: str) -> list[str]:
    """The counted words of ``text``, in order."""
    normalized = unicodedata.normalize("NFC", text)
    found: list[str] = []
    for raw_token in normalized.split():
        token = raw_token.strip(_STRIP_CHARS)
        if not token:
            continue
        if token.isalpha():
            # Every character of an all-letter token matches the word class,
            # so the token is exactly one letter run and needs no scan for it.
            if not _CAMEL_CASE_TOKEN.search(token):
                found.append(token)
            continue
        if _EXCLUDED_TOKEN.search(token):
            continue
        found.extend(_WORD.findall(token))
    return found


def count_words(text: str) -> int:
    """The deterministic word count of ``text``."""
    return len(words(text))
