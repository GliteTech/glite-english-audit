"""Deterministic privacy pattern scanner for shareable content.

This scanner is the double-protection layer under the semantic privacy audit
(specification, 8.2, 8.3): a safe record must already be safe when created,
and everything here must still pass. It deliberately over-flags: a false
positive costs one withheld record, while a false negative leaks data.

Only privacy-safe surfaces are scanned — mistake records, submission packages,
and progress text. Raw utterances are private and are never routed through
shareable-content checks.

Matching runs on a normalized copy of the text: NFKC plus removal of every
Unicode format character (category Cf). Without it a single zero-width space
inside ``alice@acme.com`` defeats every pattern here, and the character is
invisible both on the review page and to the semantic verifier, so both gates
fall together. The scanner never returns the normalized text and no caller
rewrites the record: what ships stays byte-identical to what was scanned, or
the same bypass would simply move one step downstream. Instead, text that
changes under that normalization is reported as
``PRIVACY_INVISIBLE_CHARACTER``, so a record carrying hidden characters is
withheld rather than silently rewritten.

Every pattern also runs a second time over a Latin-lookalike folding of that
normalized text. NFKC leaves Cyrillic ``а`` and Greek ``ο`` alone, so
``аcme.io`` renders as a domain, reads as a domain to the semantic verifier and
to the user, and matches none of the ASCII character classes below. Folding the
lookalikes back to Latin restores every pattern without reporting a separate
code: what the record leaks is still a domain, and that is what the diagnostic
must say.
"""

import re
import unicodedata
from dataclasses import dataclass

from glite_english_audit.artifacts.models import PUBLIC_SOURCE_TYPES, SafeMistakeRecord
from glite_english_audit.diagnostics.codes import Diagnostic

MAX_EXAMPLE_WORDS = 15

# Ordinary English abbreviations written with an internal period. They match the
# bare-domain shape and are excluded so prose is not read as a hostname.
_ABBREVIATIONS_NOT_DOMAINS: frozenset[str] = frozenset(
    {"et.al", "op.cit", "loc.cit", "sq.ft", "cu.ft", "sq.mi"}
)

# Directory names that make a slash-separated run a path rather than grammar
# notation such as "he/she/it".
_PATH_ROOT_TOKENS = (
    "bin",
    "build",
    "config",
    "dist",
    "etc",
    "lib",
    "node_modules",
    "opt",
    "packages",
    "scripts",
    "src",
    "srv",
    "tmp",
    "usr",
    "var",
    "vendor",
)

_URL = re.compile(
    # Scheme or www prefix: any case, any host.
    r"(?i:(?:https?://|www\.)\S+)"
    # Bare domain: any label plus any 2-24 letter TLD, not just a fixed list.
    # Case-insensitive on both sides. Restricting it to lowercase let 'Acme.io'
    # through untouched, and a capital letter is not an obfuscation the scanner
    # gets to reward. It costs the run-on-sentence false positive
    # ("the plan.We agreed"), which withholds one record and leaks nothing.
    r"|(?i:\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z]{2,24}\b)"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_PATH = re.compile(
    # Absolute, home-relative, or explicitly relative POSIX paths.
    r"(?:^|(?<=[\s\"'(\[]))(?:~|\.{1,2})?/[\w.-]+(?:/[\w.-]+)*"
    # Windows drive letters and UNC prefixes.
    r"|\b[A-Za-z]:[\\/][\w.\\/-]*"
    r"|\\\\[\w.-]+\\[\w.\\-]*"
    # Slash-separated segments ending in a dotted file extension.
    r"|\b[\w.-]+(?:/[\w.-]+)*/[\w-]+\.[A-Za-z0-9]{1,8}\b"
    # Slash-separated segments rooted in a well-known directory name.
    r"|\b(?:" + "|".join(_PATH_ROOT_TOKENS) + r")/[\w.-]+(?:/[\w.-]+)*",
    re.MULTILINE,
)
_CREDENTIAL = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{8,}"
    r"|\bxox[a-z]-[A-Za-z0-9-]+"
    r"|\bAKIA[A-Z0-9]{8,}"
    r"|\bAIza[A-Za-z0-9_-]{8,}"
    r"|BEGIN [A-Z ]*PRIVATE KEY"
    r"|\beyJ[A-Za-z0-9_-]{10,}"
)
_IDENTIFIER = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-f]{16,}\b"
    # Short commit hashes: eight or more hex characters mixing digits and
    # letters, which no ordinary English word does.
    r"|\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b"
    # Encoded blobs. Base64 and base32 of a URL, a path, or a key match none of
    # the patterns above, and an example is at most fifteen words of English, so
    # a 24-character unbroken run of base64 alphabet is not a word.
    r"|\b[A-Za-z0-9+/]{24,}={0,2}",
    re.IGNORECASE,
)
_CODE = re.compile(
    # A single semicolon is ordinary English punctuation; braces, operators,
    # fences, keywords, call shapes, and statement-final semicolons are code.
    r"[{}]"
    r"|;\s*$"
    r"|=>|==|!=|::|\+=|->"
    r"|```"
    r"|\b(?:def|class|import|function|return|const|var|let)\s"
    r"|\b\w+_\w+\("
    r"|\b\w+\.\w+\("
    r"|\)\s*;",
    re.MULTILINE,
)
_NUMBER = re.compile(
    # Three digits already carry an exact user count, price, or metric; one and
    # two digit numbers are ordinary English ("we waited 45 minutes").
    r"\d{3,}"
    r"|\d+(?:[.,]\d+)+"  # decimals and thousand groups
    r"|\d+\s?%"
    r"|[$€£¥]\s?\d+"
    r"|\d+\s?(?:USD|EUR|GBP)\b"
)
_CONTEXT_DEPENDENT = re.compile(
    r"\bin this (?:case|sentence|example|context)\b"
    r"|\bthe example\b"
    r"|\bthis (?:sentence|phrase|message)\b"
    r"|\babove\b"
    # Deictic 'here': leading a sentence, closing a clause, or introducing what
    # the reader is supposed to be looking at. A rule *about* the adverb, as in
    # "Place adverbs such as here and there after the verb", keeps 'here' inside
    # the clause and stays self-contained.
    r"|\bhere the\b"
    r"|\b(?:as used|shown|written|used) here\b"
    r"|(?:^|(?<=[.!?])\s+)here\b"
    r"|\bhere\s*(?=[.,;:!?]|$)",
    re.IGNORECASE,
)


# Cyrillic and Greek letters that render as their Latin counterparts in the
# fonts a review page and a terminal use. NFKC keeps them distinct, so every
# ASCII character class above misses them until they are folded back.
_LATIN_LOOKALIKES: dict[int, str] = {
    ord(source): target
    for source, target in (
        # Cyrillic lowercase.
        ("а", "a"),
        ("в", "b"),
        ("е", "e"),
        ("ѕ", "s"),
        ("і", "i"),
        ("ј", "j"),
        ("к", "k"),
        ("м", "m"),
        ("н", "h"),
        ("о", "o"),
        ("р", "p"),
        ("с", "c"),
        ("т", "t"),
        ("у", "y"),
        ("х", "x"),
        ("һ", "h"),
        ("ԁ", "d"),
        ("ԛ", "q"),
        ("ԝ", "w"),
        ("ѡ", "w"),
        ("ӏ", "l"),
        ("ғ", "f"),
        ("ԍ", "g"),
        # Cyrillic uppercase.
        ("А", "A"),
        ("В", "B"),
        ("Е", "E"),
        ("Ѕ", "S"),
        ("І", "I"),
        ("Ј", "J"),
        ("К", "K"),
        ("М", "M"),
        ("Н", "H"),
        ("О", "O"),
        ("Р", "P"),
        ("С", "C"),
        ("Т", "T"),
        ("У", "Y"),
        ("Х", "X"),
        ("Ԁ", "D"),
        ("Ԛ", "Q"),
        ("Ԝ", "W"),
        # Greek lowercase.
        ("α", "a"),
        ("ο", "o"),
        ("ν", "v"),
        ("ρ", "p"),
        ("τ", "t"),
        ("υ", "u"),
        ("κ", "k"),
        ("ι", "i"),
        ("μ", "u"),
        ("γ", "y"),
        ("χ", "x"),
        # Greek uppercase.
        ("Α", "A"),
        ("Β", "B"),
        ("Ε", "E"),
        ("Ζ", "Z"),
        ("Η", "H"),
        ("Ι", "I"),
        ("Κ", "K"),
        ("Μ", "M"),
        ("Ν", "N"),
        ("Ο", "O"),
        ("Ρ", "P"),
        ("Τ", "T"),
        ("Υ", "Y"),
        ("Χ", "X"),
    )
}


def _normalize(text: str) -> str:
    """NFKC plus removal of every Unicode format character."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(char for char in folded if unicodedata.category(char) != "Cf")


def _fold_lookalikes(text: str) -> str:
    """Map Latin-lookalike Cyrillic and Greek letters back to Latin."""
    return text.translate(_LATIN_LOOKALIKES)


@dataclass(frozen=True)
class _PatternCheck:
    code: str
    pattern: re.Pattern[str]
    label: str
    ignored_matches: frozenset[str] = frozenset()

    def matches(self, text: str) -> bool:
        return any(
            found.group(0).lower() not in self.ignored_matches
            for found in self.pattern.finditer(text)
        )


_CONTENT_CHECKS: tuple[_PatternCheck, ...] = (
    _PatternCheck(
        "PRIVACY_URL_PRESENT",
        _URL,
        "URL or domain",
        ignored_matches=_ABBREVIATIONS_NOT_DOMAINS,
    ),
    _PatternCheck("PRIVACY_EMAIL_PRESENT", _EMAIL, "email address"),
    _PatternCheck("PRIVACY_CREDENTIAL_PATTERN", _CREDENTIAL, "credential-shaped token"),
    _PatternCheck("PRIVACY_IDENTIFIER_PRESENT", _IDENTIFIER, "identifier"),
    _PatternCheck("PRIVACY_PATH_PRESENT", _PATH, "file or directory path"),
    _PatternCheck("PRIVACY_PHONE_PRESENT", _PHONE, "phone-number-like sequence"),
    _PatternCheck("PRIVACY_CODE_PRESENT", _CODE, "source-code-shaped text"),
    _PatternCheck("PRIVACY_SUSPICIOUS_NUMBER", _NUMBER, "exact quantity"),
)


def scan_text(text: str, *, item_ref: str | None = None) -> list[Diagnostic]:
    """Scan one shareable text for every forbidden content pattern.

    Matched text is never echoed into the diagnostic message: diagnostics may
    end up in logs, and logs must stay content-free.
    """
    diagnostics: list[Diagnostic] = []
    normalized = _normalize(text)
    if normalized != text:
        diagnostics.append(
            Diagnostic.from_code(
                "PRIVACY_INVISIBLE_CHARACTER",
                "invisible or non-canonical characters change this text under normalization",
                item_ref=item_ref,
            )
        )
    lookalike_folded = _fold_lookalikes(normalized)
    for check in _CONTENT_CHECKS:
        if check.matches(normalized) or check.matches(lookalike_folded):
            diagnostics.append(
                Diagnostic.from_code(
                    check.code,
                    f"a {check.label} pattern was detected in shareable content",
                    item_ref=item_ref,
                )
            )
    return diagnostics


SCANNED_RECORD_FIELDS: tuple[str, ...] = tuple(
    name for name, field in SafeMistakeRecord.model_fields.items() if field.annotation is str
)
"""Every free-text field of a shipped record, derived from the model itself.

A hand-written list is a field behind whenever the record grows one: the new
field ships unscanned and nothing fails. Deriving it means a seventh string
field is scanned the moment it exists.
"""


def scan_safe_record(record: SafeMistakeRecord, *, item_ref: str | None = None) -> list[Diagnostic]:
    """Scan one privacy-safe mistake record with field-specific rules."""
    diagnostics: list[Diagnostic] = []
    for field_name in SCANNED_RECORD_FIELDS:
        value: str = getattr(record, field_name)
        for diagnostic in scan_text(value, item_ref=item_ref):
            diagnostics.append(
                Diagnostic(
                    code=diagnostic.code,
                    severity=diagnostic.severity,
                    message=f"{diagnostic.message} (field: {field_name})",
                    item_ref=item_ref,
                    evidence_path=None,
                )
            )
    if record.source_type not in PUBLIC_SOURCE_TYPES:
        diagnostics.append(
            Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "source_type is not one of the stable public adapter IDs",
                item_ref=item_ref,
            )
        )
    if _CONTEXT_DEPENDENT.search(_fold_lookalikes(_normalize(record.rule))):
        diagnostics.append(
            Diagnostic.from_code(
                "PRIVACY_CONTEXT_DEPENDENT_RULE",
                "the rule sentence depends on hidden context and is not self-contained",
                item_ref=item_ref,
            )
        )
    # The limit holds for every example_type. example_type is asserted by a
    # skill, so exempting 'synthetic' would let one mislabel carry a long
    # verbatim source phrase through (specification, 8.2).
    word_count = len(record.example.split())
    if word_count > MAX_EXAMPLE_WORDS:
        diagnostics.append(
            Diagnostic.from_code(
                "PRIVACY_LONG_SOURCE_PHRASE",
                f"an example of {word_count} words exceeds the limit of {MAX_EXAMPLE_WORDS}",
                item_ref=item_ref,
            )
        )
    return diagnostics


def scan_version(value: str, *, item_ref: str | None = None) -> list[Diagnostic]:
    """Scan one declared version string for content that is not a version.

    ``PRIVACY_SUSPICIOUS_NUMBER`` is not applicable: a version is a dotted digit
    run by definition. Every other pattern still applies, because a free-form
    version field is otherwise a straight channel for a path, a session ID, or a
    raw sentence (specification, 8.3).
    """
    return [
        diagnostic
        for diagnostic in scan_text(value, item_ref=item_ref)
        if diagnostic.code != "PRIVACY_SUSPICIOUS_NUMBER"
    ]
