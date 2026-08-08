"""Deterministic privacy pattern scanner for shareable content.

This scanner is the double-protection layer under the semantic privacy audit
(specification, 8.2, 8.3): a safe record must already be safe when created,
and everything here must still pass. It deliberately over-flags: a false
positive costs one withheld record, while a false negative leaks data.

Only privacy-safe surfaces are scanned — mistake records, submission packages,
and progress text. Raw utterances are private and are never routed through
shareable-content checks.
"""

import re
from dataclasses import dataclass

from glite_english_audit.artifacts.models import SafeMistakeRecord
from glite_english_audit.diagnostics.codes import Diagnostic

MAX_NON_SYNTHETIC_EXAMPLE_WORDS = 15

_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|ai|dev|co|app|edu|gov|uk|de|ru|fr)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_PATH = re.compile(
    r"(?:^|\s)(?:~|/[\w.-]+/|[A-Za-z]:\\)[\w./\\-]*"
    r"|\b[\w.-]+/[\w.-]+/[\w./-]+\b"
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
    r"|\b[0-9a-f]{16,}\b",
    re.IGNORECASE,
)
_CODE = re.compile(
    r"[{};]"
    r"|=>|==|!=|::|\+=|->"
    r"|```"
    r"|\b(?:def|class|import|function|return|const|var|let)\s"
    r"|\b\w+_\w+\("
    r"|\b\w+\.\w+\("
)
_NUMBER = re.compile(
    r"\d{4,}"  # any long digit run, including years
    r"|\d+(?:[.,]\d+)+"  # decimals and thousand groups
    r"|\d+\s?%"
    r"|[$€£¥]\s?\d+"
    r"|\d+\s?(?:USD|EUR|GBP)\b"
)
_CONTEXT_DEPENDENT = re.compile(
    r"\bin this (?:case|sentence|example|context)\b"
    r"|\bhere\b"
    r"|\babove\b"
    r"|\bthe example\b"
    r"|\bthis (?:sentence|phrase|message)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _PatternCheck:
    code: str
    pattern: re.Pattern[str]
    label: str


_CONTENT_CHECKS: tuple[_PatternCheck, ...] = (
    _PatternCheck("PRIVACY_URL_PRESENT", _URL, "URL or domain"),
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
    for check in _CONTENT_CHECKS:
        if check.pattern.search(text):
            diagnostics.append(
                Diagnostic.from_code(
                    check.code,
                    f"a {check.label} pattern was detected in shareable content",
                    item_ref=item_ref,
                )
            )
    return diagnostics


def scan_safe_record(record: SafeMistakeRecord, *, item_ref: str | None = None) -> list[Diagnostic]:
    """Scan one privacy-safe mistake record with field-specific rules."""
    diagnostics: list[Diagnostic] = []
    for field_name in ("mistake", "rule", "example"):
        value = getattr(record, field_name)
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
    if _CONTEXT_DEPENDENT.search(record.rule):
        diagnostics.append(
            Diagnostic.from_code(
                "PRIVACY_CONTEXT_DEPENDENT_RULE",
                "the rule sentence depends on hidden context and is not self-contained",
                item_ref=item_ref,
            )
        )
    if record.example_type.value != "synthetic":
        word_count = len(record.example.split())
        if word_count > MAX_NON_SYNTHETIC_EXAMPLE_WORDS:
            diagnostics.append(
                Diagnostic.from_code(
                    "PRIVACY_LONG_SOURCE_PHRASE",
                    f"a {record.example_type.value} example of {word_count} words exceeds the "
                    f"limit of {MAX_NON_SYNTHETIC_EXAMPLE_WORDS}",
                    item_ref=item_ref,
                )
            )
    return diagnostics
