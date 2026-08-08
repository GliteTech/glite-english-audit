"""Privacy pattern scanner: every forbidden pattern, field labels, and limits."""

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.models import SafeMistakeRecord
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.verification.privacy_scanner import (
    MAX_NON_SYNTHETIC_EXAMPLE_WORDS,
    scan_safe_record,
    scan_text,
)


def _codes(diagnostics: list[Diagnostic]) -> set[str]:
    return {diagnostic.code for diagnostic in diagnostics}


def _record(
    *,
    mistake: str = "The learner wrote very like instead of really like.",
    rule: str = "Use really, not very, before like.",
    example: str = "I really like this plan.",
    example_type: ExampleType = ExampleType.SYNTHETIC,
) -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule=rule,
        example=example,
        example_type=example_type,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("See https://fake-site.example/docs for details.", "PRIVACY_URL_PRESENT"),
        ("Their site example.com was mentioned.", "PRIVACY_URL_PRESENT"),
        ("Contact fake.person@example.com today.", "PRIVACY_EMAIL_PRESENT"),
        ("Call +1 555 010 0199 now.", "PRIVACY_PHONE_PRESENT"),
        ("The log sat in /var/log/example-app today.", "PRIVACY_PATH_PRESENT"),
        ("Saved under C:\\Temp\\notes.txt earlier.", "PRIVACY_PATH_PRESENT"),
        ("Ref 123e4567-e89b-12d3-a456-426614174000 appeared.", "PRIVACY_IDENTIFIER_PRESENT"),
        ("Commit deadbeefdeadbeefdeadbeef was noted.", "PRIVACY_IDENTIFIER_PRESENT"),
        ("if (a == b) { log_event(x); }", "PRIVACY_CODE_PRESENT"),
        ("The invoice number 123456 was quoted.", "PRIVACY_SUSPICIOUS_NUMBER"),
        ("Usage rose 15% last week.", "PRIVACY_SUSPICIOUS_NUMBER"),
        ("The fee was $42 total.", "PRIVACY_SUSPICIOUS_NUMBER"),
    ],
)
def test_pattern_positive(text: str, expected_code: str) -> None:
    assert expected_code in _codes(scan_text(text))


@pytest.mark.parametrize(
    "token",
    [
        "sk-FAKEFAKEFAKE0000",
        "ghp_FAKEFAKEFAKE0000",
        "xoxb-FAKE-EXAMPLE-TOKEN",
        "AKIAFAKEFAKEEXAMPLE",
        "AIzaFAKE-EXAMPLE-KEY0",
        "-----BEGIN PRIVATE KEY----- FAKE EXAMPLE",
        "eyJFAKEEXAMPLEFAKE00",
    ],
)
def test_credential_positive(token: str) -> None:
    assert "PRIVACY_CREDENTIAL_PATTERN" in _codes(scan_text(f"The text contained {token} there."))


@pytest.mark.parametrize(
    "text",
    [
        "The website loaded slowly for everyone.",  # no URL or domain
        "Reach the team at the front desk.",  # no email
        "Call me when the meeting ends.",  # no phone number
        "The file lives in the project folder.",  # no path
        "The drive was full yesterday.",  # no Windows path
        "My token expired and was renewed.",  # no credential shape
        "The report used a short id.",  # no UUID
        "abcdef is a short hex run.",  # hex run shorter than the limit
        "The message read clearly without markup.",  # no code shapes
        "We fixed a few small issues.",  # no long digit run
        "Most users were satisfied.",  # no percentage
        "The plan costs very little.",  # no currency amount
    ],
)
def test_pattern_clean_negative(text: str) -> None:
    assert scan_text(text) == []


def test_scan_safe_record_labels_the_offending_field() -> None:
    record = _record(example="Write to fake.person@example.com soon.")
    diagnostics = scan_safe_record(record, item_ref="records[0]")
    email = [d for d in diagnostics if d.code == "PRIVACY_EMAIL_PRESENT"]
    assert email
    assert all("(field: example)" in d.message for d in email)
    assert all("(field: mistake)" not in d.message for d in diagnostics)
    assert all("(field: rule)" not in d.message for d in diagnostics)
    assert all(d.item_ref == "records[0]" for d in diagnostics)


@pytest.mark.parametrize(
    "rule",
    [
        "Here the verb should come first.",
        "In this case the article is missing.",
    ],
)
def test_context_dependent_rule_detected(rule: str) -> None:
    diagnostics = scan_safe_record(_record(rule=rule))
    assert "PRIVACY_CONTEXT_DEPENDENT_RULE" in _codes(diagnostics)


def test_context_words_outside_the_rule_field_do_not_trigger() -> None:
    diagnostics = scan_safe_record(_record(example="I like it here."))
    assert "PRIVACY_CONTEXT_DEPENDENT_RULE" not in _codes(diagnostics)


@pytest.mark.parametrize("example_type", [ExampleType.VERBATIM, ExampleType.REDACTED])
def test_non_synthetic_example_at_word_limit_passes(example_type: ExampleType) -> None:
    example = " ".join(["plan"] * MAX_NON_SYNTHETIC_EXAMPLE_WORDS)
    diagnostics = scan_safe_record(_record(example=example, example_type=example_type))
    assert "PRIVACY_LONG_SOURCE_PHRASE" not in _codes(diagnostics)


@pytest.mark.parametrize("example_type", [ExampleType.VERBATIM, ExampleType.REDACTED])
def test_non_synthetic_example_over_word_limit_flagged(example_type: ExampleType) -> None:
    example = " ".join(["plan"] * (MAX_NON_SYNTHETIC_EXAMPLE_WORDS + 1))
    diagnostics = scan_safe_record(_record(example=example, example_type=example_type))
    assert "PRIVACY_LONG_SOURCE_PHRASE" in _codes(diagnostics)


def test_synthetic_example_is_exempt_from_word_limit() -> None:
    example = " ".join(["plan"] * (MAX_NON_SYNTHETIC_EXAMPLE_WORDS + 1))
    diagnostics = scan_safe_record(_record(example=example, example_type=ExampleType.SYNTHETIC))
    assert "PRIVACY_LONG_SOURCE_PHRASE" not in _codes(diagnostics)


def test_diagnostics_never_echo_scanned_text() -> None:
    secret = "sk-FAKEFAKEFAKEEXAMPLE0001"
    diagnostics = scan_text(f"The note held {secret} inside.")
    assert diagnostics
    assert all(secret not in d.message for d in diagnostics)


def test_record_diagnostics_never_echo_scanned_text() -> None:
    secret = "ghp_FAKEEXAMPLEFAKE0002"
    diagnostics = scan_safe_record(_record(example=f"Token {secret} was pasted."))
    assert diagnostics
    for diagnostic in diagnostics:
        assert secret not in diagnostic.message
        assert diagnostic.evidence_path is None
