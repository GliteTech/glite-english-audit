"""Privacy pattern scanner: every forbidden pattern, field labels, and limits."""

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.models import SafeMistakeRecord
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.verification.privacy_scanner import (
    MAX_EXAMPLE_WORDS,
    scan_safe_record,
    scan_text,
)

_ZERO_WIDTH_SPACE = "\u200b"
_SOFT_HYPHEN = "\u00ad"
_FULLWIDTH_FULL_STOP = "\uff0e"


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
    example = " ".join(["plan"] * MAX_EXAMPLE_WORDS)
    diagnostics = scan_safe_record(_record(example=example, example_type=example_type))
    assert "PRIVACY_LONG_SOURCE_PHRASE" not in _codes(diagnostics)


@pytest.mark.parametrize("example_type", [ExampleType.VERBATIM, ExampleType.REDACTED])
def test_non_synthetic_example_over_word_limit_flagged(example_type: ExampleType) -> None:
    example = " ".join(["plan"] * (MAX_EXAMPLE_WORDS + 1))
    diagnostics = scan_safe_record(_record(example=example, example_type=example_type))
    assert "PRIVACY_LONG_SOURCE_PHRASE" in _codes(diagnostics)


@pytest.mark.parametrize("example_type", list(ExampleType))
def test_word_limit_applies_to_every_example_type(example_type: ExampleType) -> None:
    # example_type is asserted by a skill, so a mislabel must not disable the
    # only unconditional length defence (specification, 8.2).
    example = " ".join(["plan"] * 16)
    diagnostics = scan_safe_record(_record(example=example, example_type=example_type))
    assert "PRIVACY_LONG_SOURCE_PHRASE" in _codes(diagnostics)


def test_long_verbatim_sentence_mislabelled_synthetic_is_flagged() -> None:
    example = (
        "The onboarding team told me that our largest client cancelled several seats "
        "last quarter, so the renewal numbers for the Berlin office will look much "
        "worse than the forecast that we have presented to the board on Monday."
    )
    assert len(example.split()) == 38
    diagnostics = scan_safe_record(_record(example=example, example_type=ExampleType.SYNTHETIC))
    assert "PRIVACY_LONG_SOURCE_PHRASE" in _codes(diagnostics)


@pytest.mark.parametrize(
    "text",
    [
        "Use 'does' with he/she/it and 'do' with I/you/we/they.",
        "English word order is normally subject/verb/object.",
        "The pronouns he/she/they must agree with the noun.",
    ],
)
def test_grammar_notation_is_not_a_path(text: str) -> None:
    assert "PRIVACY_PATH_PRESENT" not in _codes(scan_text(text))


@pytest.mark.parametrize(
    "rule",
    [
        "Place adverbs such as here and there after the verb.",
        "Use 'there is' for a singular noun and 'here is' for something nearby.",
    ],
)
def test_rules_about_the_adverb_here_stay_self_contained(rule: str) -> None:
    assert "PRIVACY_CONTEXT_DEPENDENT_RULE" not in _codes(scan_safe_record(_record(rule=rule)))


@pytest.mark.parametrize(
    "rule",
    [
        "The preposition is wrong here.",
        "Here a definite article is required.",
        "The gerund belongs here, not the infinitive.",
        "As used here, the tense is wrong.",
        "The pattern shown here needs a plural verb.",
    ],
)
def test_deictic_here_is_still_context_dependent(rule: str) -> None:
    assert "PRIVACY_CONTEXT_DEPENDENT_RULE" in _codes(scan_safe_record(_record(rule=rule)))


@pytest.mark.parametrize(
    "domain",
    [
        "mycompany.se",
        "mycompany.nl",
        "mycompany.jp",
        "mycompany.cn",
        "mycompany.it",
        "mycompany.in",
        "mycompany.br",
        "mycompany.info",
        "mycompany.xyz",
        "mycompany.cloud",
    ],
)
def test_bare_domain_with_any_tld_is_flagged(domain: str) -> None:
    assert "PRIVACY_URL_PRESENT" in _codes(scan_text(f"The team moved to {domain} last week."))


@pytest.mark.parametrize(
    "text",
    [
        "The sources are listed et.al style in the report.",
        "The room measures about 30 sq.ft in total.",
    ],
)
def test_ordinary_abbreviations_are_not_domains(text: str) -> None:
    assert "PRIVACY_URL_PRESENT" not in _codes(scan_text(text))


def test_three_digit_count_is_a_suspicious_number() -> None:
    assert "PRIVACY_SUSPICIOUS_NUMBER" in _codes(scan_text("we lost 312 users last month"))


def test_short_commit_hash_is_an_identifier() -> None:
    assert "PRIVACY_IDENTIFIER_PRESENT" in _codes(scan_text("The fix landed in commit a3f9bd21."))


def test_zero_width_space_does_not_defeat_the_patterns() -> None:
    text = (
        f"Write to alice{_ZERO_WIDTH_SPACE}@acme{_ZERO_WIDTH_SPACE}.com when it depends from input."
    )
    codes = _codes(scan_text(text))
    assert "PRIVACY_EMAIL_PRESENT" in codes
    assert "PRIVACY_URL_PRESENT" in codes
    assert "PRIVACY_INVISIBLE_CHARACTER" in codes


@pytest.mark.parametrize("invisible", [_ZERO_WIDTH_SPACE, _SOFT_HYPHEN, "\ufeff", "\u200d"])
def test_invisible_characters_are_reported(invisible: str) -> None:
    text = f"Their site exam{invisible}ple.com was mentioned."
    codes = _codes(scan_text(text))
    assert "PRIVACY_INVISIBLE_CHARACTER" in codes
    assert "PRIVACY_URL_PRESENT" in codes


def test_soft_hyphen_does_not_defeat_the_credential_pattern() -> None:
    codes = _codes(scan_text(f"The note held sk-FAKE{_SOFT_HYPHEN}FAKEFAKE0000 inside."))
    assert "PRIVACY_CREDENTIAL_PATTERN" in codes


def test_fullwidth_full_stop_does_not_defeat_the_domain_pattern() -> None:
    codes = _codes(scan_text(f"Their site example{_FULLWIDTH_FULL_STOP}com was mentioned."))
    assert "PRIVACY_URL_PRESENT" in codes
    assert "PRIVACY_INVISIBLE_CHARACTER" in codes


def test_scanned_record_is_never_rewritten() -> None:
    example = f"Write to alice{_ZERO_WIDTH_SPACE}@acme{_ZERO_WIDTH_SPACE}.com now."
    record = _record(example=example)
    diagnostics = scan_safe_record(record)
    assert "PRIVACY_INVISIBLE_CHARACTER" in _codes(diagnostics)
    assert "PRIVACY_EMAIL_PRESENT" in _codes(diagnostics)
    # The record that would ship stays byte-identical to the scanned text.
    assert record.example == example


def test_plain_ascii_record_reports_no_invisible_character() -> None:
    assert "PRIVACY_INVISIBLE_CHARACTER" not in _codes(scan_safe_record(_record()))


def test_source_type_outside_the_known_adapter_ids_is_flagged() -> None:
    leaky = "acme_health_oncology_billing_migration_q3_client_novartis"
    record = _record().model_copy(update={"source_type": leaky})
    diagnostics = scan_safe_record(record, item_ref="records[0]")
    assert "SCHEMA_INVALID_VALUE" in _codes(diagnostics)
    assert all(leaky not in diagnostic.message for diagnostic in diagnostics)


def test_source_type_is_scanned_for_forbidden_patterns() -> None:
    record = _record().model_copy(update={"source_type": "/Users/alice/work/acme"})
    diagnostics = scan_safe_record(record)
    path = [d for d in diagnostics if d.code == "PRIVACY_PATH_PRESENT"]
    assert path
    assert all("(field: source_type)" in diagnostic.message for diagnostic in path)


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


@pytest.mark.parametrize(
    "domain",
    ["Acme.io", "ACME.io", "MyCompany.dev", "Acme.IO", "ACME.IO", "Acme.Io"],
)
def test_capitalized_bare_domain_is_flagged(domain: str) -> None:
    # A shift key is not an obfuscation technique; a domain identifies its owner
    # in whatever case it was typed.
    assert "PRIVACY_URL_PRESENT" in _codes(scan_text(f"The team moved to {domain} last week."))


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        # Cyrillic а, е, о, с, р, х and Greek ο look like their Latin twins and
        # survive NFKC unchanged, so nothing else in the scanner sees them.
        ("The team moved to аcme.io last week.", "PRIVACY_URL_PRESENT"),
        ("Write to аlice@аcme.io today.", "PRIVACY_EMAIL_PRESENT"),
        ("The log sat in /vаr/log/acme-app today.", "PRIVACY_PATH_PRESENT"),
        ("The note held ѕk-FAKEFAKEFAKE0000 inside.", "PRIVACY_CREDENTIAL_PATTERN"),
        ("Their site οnboarding.dev was mentioned.", "PRIVACY_URL_PRESENT"),
    ],
)
def test_latin_lookalike_characters_do_not_defeat_the_patterns(
    text: str, expected_code: str
) -> None:
    assert expected_code in _codes(scan_text(text))


@pytest.mark.parametrize(
    "blob",
    [
        # base64 and base32 of "https://acme-internal.example/team/roadmap".
        "aHR0cHM6Ly9hY21lLWludGVybmFsLmV4YW1wbGUvdGVhbS9yb2FkbWFw",
        "aHR0cHM6Ly9hY21lLWludGVybmFsLmV4YW1wbGUvdGVhbS9yb2FkbWFw=",
        "NB2HI4DTHIXS443JNZTXI3TBNZUWK3TDPFZXIZLSOMXGC3LFOJUW4ZY",
    ],
)
def test_an_encoded_blob_is_an_identifier(blob: str) -> None:
    # Encoding a URL or a path defeats every shape-based pattern; the blob
    # itself is the giveaway.
    assert "PRIVACY_IDENTIFIER_PRESENT" in _codes(scan_text(f"The learner wrote {blob} once."))


def test_ordinary_english_is_never_an_encoded_blob() -> None:
    assert scan_text("The learner wrote a very long uncountable noun incorrectly.") == []


def test_lookalike_characters_leave_the_record_untouched() -> None:
    example = "The team moved to аcme.io last week."
    record = _record(example=example)
    assert "PRIVACY_URL_PRESENT" in _codes(scan_safe_record(record))
    assert record.example == example


def test_every_text_field_of_a_shipped_record_is_scanned() -> None:
    # A seventh string field added to SafeMistakeRecord must be scanned by
    # construction; a hand-maintained field list ships the new one unchecked.
    text_fields = {
        name for name, field in SafeMistakeRecord.model_fields.items() if field.annotation is str
    }
    for field_name in text_fields:
        record = _record().model_copy(update={field_name: "Write to fake.person@example.com."})
        codes = _codes(scan_safe_record(record))
        assert "PRIVACY_EMAIL_PRESENT" in codes, f"{field_name} is not scanned"
