"""Review page rendering: records, confirmations, package bytes, and modes."""

import html
import re

from glite_english_audit.artifacts.enums import ExampleType, Modality, StageId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import (
    canonical_json_bytes,
    new_artifact_id,
    new_run_id,
)
from glite_english_audit.artifacts.models import (
    AuditCounts,
    ModalityCounts,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
)
from glite_english_audit.review_server.page import render_page
from glite_english_audit.review_server.session import ReviewSessionState
from glite_english_audit.submission.capability import SubmissionCapability

_FAKE_TOKEN = "review-token-FAKE-EXAMPLE"


def _written_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=800,
        analyzed_words=750,
        eligible_utterances=60,
        analyzed_utterances=55,
    )


def _spoken_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=400,
        analyzed_words=350,
        eligible_utterances=30,
        analyzed_utterances=25,
    )


def _safe_record(*, mistake: str, rule: str, example: str) -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule=rule,
        example=example,
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _artifact() -> ReviewedSubmissionArtifact:
    records = [
        ReviewedRecord(
            mistake_id="m-1",
            record=_safe_record(
                mistake="Wrote 'more easy' instead of 'easier'.",
                rule="Short adjectives form the comparative with -er.",
                example="This route is easier than the old one.",
            ),
            included=True,
            privacy_creator_version="0.1.0",
            privacy_verifier_version="0.1.0",
        ),
        ReviewedRecord(
            mistake_id="m-2",
            record=_safe_record(
                mistake="Wrote 'informations' instead of 'information'.",
                rule="The noun 'information' is uncountable.",
                example="She gave me useful information about the city.",
            ),
            included=True,
            privacy_creator_version="0.1.0",
            privacy_verifier_version="0.1.0",
        ),
    ]
    counts = AuditCounts(
        eligible_english_words=1200,
        analyzed_english_words=1100,
        eligible_utterances=90,
        analyzed_utterances=80,
        written=_written_modality(),
        spoken_asr=_spoken_modality(),
        verified_total_mistakes=3,
        shared_mistakes=2,
        withheld_by_user=0,
        withheld_for_privacy=1,
    )
    envelope = ArtifactEnvelope(
        schema_name="reviewed_submission",
        schema_version=1,
        artifact_id=new_artifact_id(),
        run_id=new_run_id(),
        stage_id=StageId.REVIEWED_SUBMISSION,
        producer_name="test",
        producer_version="0.1.0",
        created_at=utc_now(),
    )
    return ReviewedSubmissionArtifact(envelope=envelope, records=records, counts=counts)


def _state() -> ReviewSessionState:
    return ReviewSessionState(_artifact())


def _download_only() -> SubmissionCapability:
    return SubmissionCapability(
        direct_submission_available=False,
        reason="No Glite submission endpoint is configured. "
        "Save the package and upload it later on the Glite website.",
    )


def _direct() -> SubmissionCapability:
    return SubmissionCapability(
        direct_submission_available=True,
        reason="A compatible Glite endpoint is configured.",
        endpoint_base_url="https://glite-EXAMPLE.invalid",
    )


def _input_tag(page: str, element_id: str) -> str:
    match = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', page)
    assert match is not None, f"no input with id {element_id!r}"
    return match.group(0)


def test_page_contains_every_record_field() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    for text in (
        "Wrote 'more easy' instead of 'easier'.",
        "Short adjectives form the comparative with -er.",
        "This route is easier than the old one.",
        "Wrote 'informations' instead of 'information'.",
        "The noun 'information' is uncountable.",
        "She gave me useful information about the city.",
    ):
        assert html.escape(text, quote=True) in page
    assert "claude_code" in page
    assert "written" in page
    assert "synthetic" in page


def test_record_checkboxes_are_checked_by_default() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    toggles = re.findall(r'<input[^>]*class="record-toggle"[^>]*>', page)
    assert len(toggles) == 2
    for tag in toggles:
        assert " checked" in tag


def test_confirmations_are_present_and_unchecked() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    adult = _input_tag(page, "adult-confirmed")
    storage = _input_tag(page, "storage-confirmed")
    assert " checked" not in adult
    assert " checked" not in storage
    assert "at least 18 years old" in page
    assert "permanent, irrevocable storage" in page
    assert "external AI processing" in page


def test_counts_summary_lines() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "Eligible English words analyzed" in page
    assert "1100 of 1200" in page
    assert "Eligible utterances analyzed" in page
    assert "80 of 90" in page
    assert "Verified mistakes" in page
    assert "Could not be made safe to share" in page


def test_exclusion_semantics_are_explained() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "still counts as one withheld mistake" in page
    assert "you cannot edit them" in page


def test_will_send_line_shows_included_count() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "Will send" in page
    assert 'id="will-send-count">2<' in page


def test_page_shows_package_bytes_matching_state() -> None:
    state = _state()
    page = render_page(state, _download_only(), _FAKE_TOKEN)
    package = state.current_package()
    assert package is not None
    package_text = canonical_json_bytes(package.model_dump(mode="json")).decode("utf-8")
    assert html.escape(package_text, quote=True) in page
    assert package.submission_id in page


def test_download_only_page_has_no_send_button_and_save_sentence() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert re.search(r'<button[^>]*id="send-button"', page) is None
    assert "anonymously" not in page
    assert "upload it later on the Glite website" in page
    assert "Download the package" in page


def test_direct_mode_send_button_disabled_and_labeled() -> None:
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    assert "download-only-note" not in page
    button_match = re.search(r"<button[^>]*id=\"send-button\"[^>]*>", page)
    assert button_match is not None
    assert "disabled" in button_match.group(0)
    assert 'id="send-count">2<' in page
    assert "mistakes anonymously" in page


def test_excluded_record_renders_unchecked_and_lowers_count() -> None:
    state = _state()
    state.set_included("m-1", False)
    page = render_page(state, _download_only(), _FAKE_TOKEN)
    toggle = _input_tag(page, "include-0")
    assert " checked" not in toggle
    assert 'id="will-send-count">1<' in page
    assert 'id="withheld-user-count">1<' in page


def test_zero_included_shows_empty_package_message() -> None:
    state = _state()
    state.set_included("m-1", False)
    state.set_included("m-2", False)
    page = render_page(state, _download_only(), _FAKE_TOKEN)
    assert "there is no package to send" in page
    assert "Download the package" in page


def test_single_style_block_and_both_themes() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert page.count("<style>") == 1
    assert "prefers-color-scheme: dark" in page
    assert 'name="color-scheme" content="light dark"' in page
    assert "#020306" in page
    assert "#FBFCFF" in page
    assert "#005BFF" in page


def test_no_external_references_or_editing_controls() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "https://" not in page
    assert "http://" not in page
    assert "src=" not in page
    assert "<textarea" not in page
    assert 'type="text"' not in page
    assert ":focus-visible" in page


def test_token_is_embedded_for_the_csrf_header() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert f'data-token="{_FAKE_TOKEN}"' in page
    assert "X-Glite-Review" in page
