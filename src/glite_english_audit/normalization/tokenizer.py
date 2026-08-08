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

_URL_TOKEN = re.compile(r"://|^www\.", re.IGNORECASE)
_EMAIL_TOKEN = re.compile(r"@[\w-]+\.")
_PATH_TOKEN = re.compile(r"^(?:/|~|\.{1,2}/|[A-Za-z]:[\\/])|[\\/].*[\\/]")
_FILENAME_TOKEN = re.compile(r"^[\w-]+\.[A-Za-z]\w{0,3}$")
_CAMEL_CASE_TOKEN = re.compile(r"[a-z][A-Z][a-z]")
_CODE_CHARS = frozenset("_{}()[];=<>`$#|&*%^")

# Punctuation commonly attached to a word in prose; stripped before the token
# is classified so "time." or "(really)" is judged as its inner word.
_STRIP_CHARS = "\"'“”‘’.,!?;:()[]{}<>…«»—–-*`~"


def _token_is_excluded(token: str) -> bool:
    if _URL_TOKEN.search(token):
        return True
    if _EMAIL_TOKEN.search(token):
        return True
    if _PATH_TOKEN.search(token):
        return True
    if _FILENAME_TOKEN.fullmatch(token):
        return True
    if _CAMEL_CASE_TOKEN.search(token):
        return True
    return any(char in _CODE_CHARS for char in token)


def words(text: str) -> list[str]:
    """The counted words of ``text``, in order."""
    normalized = unicodedata.normalize("NFC", text)
    found: list[str] = []
    for raw_token in normalized.split():
        token = raw_token.strip(_STRIP_CHARS)
        if not token or _token_is_excluded(token):
            continue
        found.extend(_WORD.findall(token))
    return found


def count_words(text: str) -> int:
    """The deterministic word count of ``text``."""
    return len(words(text))
