"""Review page rendering: records, confirmations, package bytes, modes, and access.

The accessibility assertions here are the release gate for specification 12.4.
Contrast is computed from the rendered palette, not asserted by eye, so a
future color change cannot silently drop below WCAG 2.2 AA.
"""

import html
import itertools
import re

from glite_english_audit.artifacts.enums import ExampleType, Modality, StepId
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
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.review_server.page import (
    EXAMPLE_ORIGIN_LABELS,
    PRIVACY_POLICY_URL,
    SKIP_LINK_TEXT,
    STORAGE_CONFIRMATION_TEXT,
    TERMS_URL,
    render_page,
)
from glite_english_audit.review_server.report_handoff import REPORT_PAGE_URL
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


def _safe_record(
    *,
    mistake: str,
    rule: str,
    example: str,
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
        step_id=StepId.E_VERIFIED,
        producer_name="test",
        producer_version="0.1.0",
        created_at=utc_now(),
    )
    return ReviewedSubmissionArtifact(envelope=envelope, records=records, counts=counts)


def _state() -> ReviewSessionState:
    return ReviewSessionState(_artifact())


def _confirmed_state() -> ReviewSessionState:
    state = _state()
    state.set_adult_confirmed(True)
    state.set_storage_confirmed(True)
    return state


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


def _tag(page: str, name: str, element_id: str) -> str:
    match = re.search(rf'<{name}[^>]*id="{element_id}"[^>]*>', page)
    assert match is not None, f"no <{name}> with id {element_id!r}"
    return match.group(0)


def _page_direct() -> str:
    return render_page(_state(), _direct(), _FAKE_TOKEN)


def _style(page: str) -> str:
    match = re.search(r"<style>(.*?)</style>", page, re.DOTALL)
    assert match is not None, "the page has no style block"
    return match.group(1)


def _script(page: str) -> str:
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None, "the page has no script block"
    return match.group(1)


def _rule_body(style: str, selector: str) -> str:
    """The declaration block of one top-level rule, selected exactly."""
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", style)
    assert match is not None, f"no top-level rule for {selector!r}"
    return match.group(1)


def _declarations(style: str, selector: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _rule_body(style, selector).split(";"):
        name, separator, value = part.partition(":")
        if separator:
            out[name.strip()] = value.strip()
    return out


def _block_span(style: str, header: str) -> tuple[int, int]:
    """Character span of a brace-balanced block starting at ``header``."""
    start = style.index(header)
    depth = 0
    for index in range(style.index("{", start), len(style)):
        if style[index] == "{":
            depth += 1
        elif style[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise AssertionError(f"unbalanced block for {header!r}")


# --- color math (WCAG 2.x relative luminance and contrast) -------------------

_HEX = re.compile(r"^#([0-9A-Fa-f]{6})$")
_RGB = re.compile(
    r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+)\s*)?\)$",
)

Rgb = tuple[int, int, int]


def _parse_color(value: str) -> tuple[Rgb, float]:
    value = value.strip()
    hex_match = _HEX.match(value)
    if hex_match is not None:
        digits = hex_match.group(1)
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)), 1.0
    rgb_match = _RGB.match(value)
    assert rgb_match is not None, f"unparsable color {value!r}"
    alpha = float(rgb_match.group(4)) if rgb_match.group(4) is not None else 1.0
    return (
        int(rgb_match.group(1)),
        int(rgb_match.group(2)),
        int(rgb_match.group(3)),
    ), alpha


def _composite(color: Rgb, alpha: float, backdrop: Rgb) -> Rgb:
    return (
        round(alpha * color[0] + (1.0 - alpha) * backdrop[0]),
        round(alpha * color[1] + (1.0 - alpha) * backdrop[1]),
        round(alpha * color[2] + (1.0 - alpha) * backdrop[2]),
    )


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(color: Rgb) -> float:
    red, green, blue = (_channel(part / 255.0) for part in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: Rgb, background: Rgb) -> float:
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _palettes(page: str) -> dict[str, dict[str, str]]:
    """The light palette and the dark palette after its overrides are applied."""
    blocks = re.findall(r":root\s*\{([^}]*)\}", _style(page))
    assert len(blocks) == 2, "expected exactly one light :root block and one dark override"
    light = dict(re.findall(r"(--[a-z-]+)\s*:\s*([^;]+);", blocks[0]))
    dark = dict(light)
    dark.update(re.findall(r"(--[a-z-]+)\s*:\s*([^;]+);", blocks[1]))
    return {"light": light, "dark": dark}


def _resolved(palette: dict[str, str], token: str, backdrop: Rgb | None = None) -> Rgb:
    color, alpha = _parse_color(palette[token])
    if alpha >= 1.0:
        return color
    assert backdrop is not None, f"{token} is translucent and needs a backdrop"
    return _composite(color, alpha, backdrop)


# Text pairs need 4.5:1; the page has no large text. Non-text pairs are borders,
# surfaces, and focus rings, which need 3:1. --line is deliberately absent: the
# row rules between records are decoration, and every record is identified by
# its own labeled checkbox rather than by the line above it.
_TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("--ink", "--bg"),
    ("--ink-soft", "--bg"),
    ("--action-text", "--bg"),
    ("--on-action", "--action"),
    ("--ok", "--bg"),
    ("--fail", "--bg"),
    ("--ink", "--well"),
    ("--ink-soft", "--well"),
)
_NON_TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("--action", "--bg"),
    ("--focus", "--bg"),
    ("--line-strong", "--bg"),
    ("--action-text", "--bg"),
    ("--ink-soft", "--bg"),
    ("--ok", "--bg"),
    ("--fail", "--bg"),
)


# --- content ----------------------------------------------------------------


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
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    adult = _input_tag(page, "adult-confirmed")
    storage = _input_tag(page, "storage-confirmed")
    assert " checked" not in adult
    assert " checked" not in storage
    assert "at least 18 years old" in page
    # Assert the constants render, not their wording -- this sentence has been
    # rewritten three times and will be again. What may not be compressed away is
    # any commitment that changes whether a reasonable person agrees.
    assert html.escape(STORAGE_CONFIRMATION_TEXT, quote=True) in page
    for commitment in ("anonymous", "permanently", "cannot delete", "train", "outside AI model"):
        assert commitment in STORAGE_CONFIRMATION_TEXT
    # And it must point somewhere the detail can actually be read.
    assert "Terms" in STORAGE_CONFIRMATION_TEXT


def test_report_handoff_page_includes_unchecked_confirmations() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    adult = _input_tag(page, "adult-confirmed")
    storage = _input_tag(page, "storage-confirmed")
    assert " checked" not in adult
    assert " checked" not in storage
    assert "Both are required" in page


def test_counts_summary_lines() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "Your words checked" in page
    # Some writing could not be read, so the shortfall is worth stating.
    assert "1,100 of 1,200" in page
    assert "Mistakes found" in page
    assert "Held back for privacy" in page


def test_a_complete_read_states_one_number_not_a_ratio_against_itself() -> None:
    """The ordinary run read everything, and said "2183 of 2183".

    A number repeated against itself reads as a ratio worth checking and turns
    out to carry nothing, which costs more attention than the shortfall the pair
    exists to disclose. The message count went entirely: it answered a question
    nobody reviewing their own sentences is asking.
    """
    from glite_english_audit.review_server.page import _analyzed

    assert _analyzed(2183, 2183) == "2,183"
    assert _analyzed(1100, 1200) == "1,100 of 1,200"


def test_exclusion_semantics_are_explained() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    # Whatever the wording, the page must say that unchecking sends nothing of
    # that record. It is one line in the records preamble now rather than a
    # paragraph of its own -- three blocks of rules stood between the reader and
    # the first record they were meant to judge.
    assert "sends no part of it" in page


def test_will_send_line_shows_included_count() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert re.search(r'<span id="will-send-count">2</span> of 2 selected', page)
    assert 'id="will-send-count">2<' in page


def test_record_count_and_its_noun_agree_in_the_plural() -> None:
    page = _page_direct()
    assert '<span id="send-count">2</span> <span id="send-noun">mistakes</span> anonymously' in page


def test_record_count_and_its_noun_agree_in_the_singular() -> None:
    """One record is a mistake. A tool that grades agreement must get this right."""
    state = _state()
    state.set_included("m-1", False)
    page = render_page(state, _direct(), _FAKE_TOKEN)
    assert '<span id="send-count">1</span> <span id="send-noun">mistake</span> anonymously' in page
    assert "mistakes</span> anonymously" not in page


def test_the_script_changes_the_noun_whenever_it_changes_the_count() -> None:
    """The server's agreement is worthless if a checkbox restores '1 mistakes'."""
    script = _script(_page_direct())
    assert 'count === 1 ? "mistake" : "mistakes"' in script
    assert 'setNoun("send-noun", data.will_send)' in script


def test_page_shows_package_bytes_matching_state() -> None:
    state = _state()
    page = render_page(state, _download_only(), _FAKE_TOKEN)
    package = state.current_package()
    assert package is not None
    package_text = canonical_json_bytes(package.model_dump(mode="json")).decode("utf-8")
    assert html.escape(package_text, quote=True) in page
    assert package.submission_id in page


def test_exact_json_is_inside_a_closed_disclosure() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert '<details><summary id="package-heading">View exact submission JSON</summary>' in page
    assert "<details open>" not in page
    assert page.index('id="download-link"') < page.index('id="package-heading"')


def test_download_only_page_has_report_and_download_but_no_direct_send() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert re.search(r'<button[^>]*id="send-button"', page) is None
    assert "anonymously" not in page
    assert "Create report" in page
    assert "upload it whenever you like" in page
    assert "Download package" in page


def test_report_form_posts_the_exact_package_and_consent_outside_it() -> None:
    state = _state()
    page = render_page(state, _download_only(), _FAKE_TOKEN)
    package = state.current_package_bytes()
    assert package is not None

    form = _tag(page, "form", "report-form")
    assert f'action="{REPORT_PAGE_URL}"' in form
    assert 'method="post"' in form
    assert 'accept-charset="UTF-8"' in form
    assert "target=" not in form

    submission = _input_tag(page, "report-submission")
    package_text = package.decode("utf-8")
    assert 'name="submission"' in submission
    assert f'value="{html.escape(package_text, quote=True)}"' in submission
    for name in (
        "adult_attested",
        "permanent_storage_and_uses_accepted",
        "external_ai_processing_accepted",
    ):
        assert re.search(rf'<input[^>]*name="{name}"[^>]*value="true"[^>]*>', page)
    # Whatever the version is, the form must carry the one the page was rendered
    # with, so a package can never claim consent under wording nobody saw.
    assert re.search(
        rf'<input[^>]*name="consent_policy_version"[^>]*value="{CONSENT_POLICY_VERSION}"[^>]*>',
        page,
    )
    assert 'name="client_confirmation_at" value=""' in page


def test_report_button_is_the_primary_action_and_starts_blocked() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    report = _tag(page, "button", "report-button")
    assert 'type="submit"' in report
    assert 'class="button primary"' in report
    assert 'aria-disabled="true"' in report
    assert page.index('id="report-button"') < page.index('id="download-link"')

    ready = render_page(_confirmed_state(), _download_only(), _FAKE_TOKEN)
    assert 'aria-disabled="false"' in _tag(ready, "button", "report-button")
    assert _page_direct().count('class="button primary"') == 1


def test_direct_mode_send_button_blocked_and_labeled() -> None:
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    assert "download-only-note" not in page
    button = _tag(page, "button", "send-button")
    assert 'aria-disabled="true"' in button
    assert 'id="send-count">2<' in page
    assert '<span id="send-noun">mistakes</span> anonymously' in page


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
    assert "Download package" in page


def test_single_style_block_and_both_themes() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert page.count("<style>") == 1
    assert "prefers-color-scheme: dark" in page
    assert 'name="color-scheme" content="light dark"' in page
    assert "#020306" in page
    assert "#FBFCFF" in page
    assert "#005BFF" in page


def test_the_page_fetches_nothing_from_outside_on_its_own() -> None:
    """Rendering must make no outbound request. Clicking may.

    This counted "https://" occurrences and required exactly one. That conflated
    two different things: a resource the browser loads by itself, which is the
    privacy risk, and a link the reader chooses to follow, which is how they read
    the terms they are being asked to accept. The check is now on the mechanism.
    """
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert REPORT_PAGE_URL in page
    assert "http://" not in page
    assert "src=" not in page  # nothing is loaded from anywhere
    assert "<textarea" not in page
    assert 'type="text"' not in page
    assert ":focus-visible" in page


def test_every_outbound_link_hides_the_tokenized_address() -> None:
    """The page's own URL carries the review token, so it must never travel.

    Following a link would otherwise send this address to glite.ai in the
    Referer header, handing an external site a working handle on the local
    review session.
    """
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    outbound = re.findall(r"<a\b[^>]*href=\"https://[^>]*>", page)
    assert outbound, "the terms and privacy links must be on the page"
    for tag in outbound:
        assert 'rel="noopener noreferrer"' in tag, tag
        assert 'target="_blank"' in tag, tag


def test_the_agreement_can_be_read_before_it_is_accepted() -> None:
    """The storage checkbox asks for permanent storage and model training.

    Until now the sentence beside it was the entire disclosure: there was
    nowhere to go and read what was being accepted.
    """
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert PRIVACY_POLICY_URL in page
    assert TERMS_URL in page
    assert "Privacy Policy" in page
    assert "Terms and Conditions" in page


def test_token_is_embedded_for_the_csrf_header() -> None:
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert f'data-token="{_FAKE_TOKEN}"' in page
    assert "X-Glite-Review" in page


def test_record_text_is_never_written_as_markup_by_the_script() -> None:
    script = _script(render_page(_state(), _direct(), _FAKE_TOKEN))
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script


# --- structure and semantics (specification 12.4) ---------------------------


def test_document_has_one_main_landmark_and_a_language() -> None:
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    assert '<html lang="en">' in page
    assert page.count("<main>") == 1
    assert page.count("</main>") == 1
    assert "<title>Create your English report \u2014 Glite</title>" in page


def test_heading_levels_start_at_one_and_never_skip() -> None:
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    levels = [int(level) for level in re.findall(r"<h([1-6])\b", page)]
    assert levels, "the page has no headings"
    assert levels.count(1) == 1
    assert levels[0] == 1
    for previous, current in itertools.pairwise(levels):
        assert current <= previous + 1, f"heading level jumps from {previous} to {current}"


def test_record_list_keeps_list_semantics_despite_list_style_none() -> None:
    page = render_page(_state(), _direct(), _FAKE_TOKEN)
    assert "list-style: none" in _style(page)
    assert '<ul class="records" role="list">' in page
    assert page.count('<li class="record">') == 2


def test_description_lists_are_not_laid_out_directly_as_grids() -> None:
    # Grid or flex applied to a <dl> element drops description-list semantics
    # in some browsers, so the layout lives on row wrappers instead.
    style = _style(_page_direct())
    for selector in ("dl.summary", "dl.record-fields"):
        assert "display" not in _declarations(style, selector)
    assert "display: inline-flex" in _rule_body(style, "dl.summary .row")
    assert "display: grid" in _rule_body(style, "dl.record-fields .row")


def test_every_checkbox_has_a_unique_programmatic_label() -> None:
    page = _page_direct()
    inputs = re.findall(r'<input[^>]*type="checkbox"[^>]*>', page)
    assert len(inputs) == 4
    labels: list[str] = []
    for tag in inputs:
        assert 'type="checkbox"' in tag
        identifier = re.search(r'id="([^"]+)"', tag)
        assert identifier is not None, f"checkbox without an id: {tag}"
        label = re.search(
            rf'<label[^>]*for="{identifier.group(1)}"[^>]*>(.*?)</label>', page, re.DOTALL
        )
        assert label is not None, f"no label bound to {identifier.group(1)!r}"
        assert label.group(1).strip(), "an empty label names nothing"
        labels.append(label.group(1))
    assert len(set(labels)) == len(labels), "two controls share the same label text"


def test_record_checkbox_label_is_the_submitted_example() -> None:
    page = _page_direct()
    for index, example in enumerate(
        (
            "This route is easier than the old one.",
            "She gave me useful information about the city.",
        )
    ):
        assert f'<label class="record-example" id="record-{index}-example"' in page
        assert f"{example}</label>" in page
        assert f"Include mistake {index + 1} of 2:" in page


def _every_example_type_state() -> ReviewSessionState:
    """One included record per ``ExampleType``, in enum order."""
    base = _artifact()
    records = [
        ReviewedRecord(
            mistake_id=f"m-{position}",
            record=_safe_record(
                mistake="Wrote 'more easy' instead of 'easier'.",
                rule="Short adjectives form the comparative with -er.",
                example=f"Sentence number {position}.",
                example_type=example_type,
            ),
            included=True,
            privacy_creator_version="0.1.0",
            privacy_verifier_version="0.1.0",
        )
        for position, example_type in enumerate(ExampleType)
    ]
    counts = base.counts.model_copy(
        update={
            "shared_mistakes": len(records),
            "verified_total_mistakes": len(records) + base.counts.withheld_for_privacy,
        }
    )
    return ReviewSessionState(
        ReviewedSubmissionArtifact(envelope=base.envelope, records=records, counts=counts)
    )


def test_each_record_row_names_where_its_example_came_from() -> None:
    # The row is where the user decides. Consenting to send a sentence you wrote
    # and consenting to send an invented one carrying the same error are
    # different decisions, so the provenance cannot live only behind the info
    # button.
    page = render_page(_every_example_type_state(), _download_only(), _FAKE_TOKEN)
    rows = re.findall(r'<div class="record-text">(.*?)</div>', page, re.DOTALL)
    assert len(rows) == len(ExampleType)
    for row, expected in zip(rows, ("your words", "your words, changed", "invented"), strict=True):
        assert f'<span class="record-origin">{expected}</span>' in row


def test_every_example_type_has_a_row_label() -> None:
    # A missing key would raise at render time rather than print a blank marker,
    # but only for the type that is missing, on a run that may be months away.
    for example_type in ExampleType:
        assert EXAMPLE_ORIGIN_LABELS[example_type].strip()
    assert len(set(EXAMPLE_ORIGIN_LABELS.values())) == len(ExampleType)


def test_info_disclosure_contains_all_record_fields() -> None:
    page = _page_direct()
    for index in range(2):
        button = _tag(page, "button", f"record-{index}-info")
        assert 'aria-expanded="false"' in button
        assert f'aria-controls="record-{index}-details"' in button
        panel = _tag(page, "div", f"record-{index}-details")
        assert 'role="region"' in panel
        assert " hidden" in panel
    for term in ("Mistake", "Rule", "Example", "Example type", "Source", "Modality"):
        assert f"<dt>{term}</dt>" in page


def test_every_aria_and_label_reference_resolves() -> None:
    for page in (_page_direct(), render_page(_state(), _download_only(), _FAKE_TOKEN)):
        known = set(re.findall(r'\bid="([^"]+)"', page))
        references: list[str] = []
        for attribute in ("aria-labelledby", "aria-describedby", "for"):
            for value in re.findall(rf'\b{attribute}="([^"]+)"', page):
                references.extend(value.split())
        references.extend(re.findall(r'href="#([^"]+)"', page))
        assert references
        missing = sorted(name for name in references if name not in known)
        assert not missing, f"dangling references: {missing}"


def test_live_count_region_announces_the_whole_sentence() -> None:
    page = _page_direct()
    region = _tag(page, "p", "will-send")
    assert 'aria-live="polite"' in region
    assert 'aria-atomic="true"' in region
    assert 'id="will-send-count"' in page
    # The atomic region is what makes a bare number meaningful when it changes.
    assert re.search(r'id="will-send"[^>]*><span id="will-send-count">2</span> of 2 selected', page)


def test_status_region_stays_in_the_accessibility_tree() -> None:
    page = _page_direct()
    assert '<p id="submit-status" role="status"></p>' in page
    style = _style(page)
    assert "#submit-status:empty" not in style
    declarations = _declarations(style, "#submit-status")
    assert declarations.get("display") != "none"
    assert "min-height" in declarations


def test_status_outcomes_are_worded_and_shaped_not_only_colored() -> None:
    page = _page_direct()
    script = _script(page)
    assert '"Sent. Submission ID: "' in script
    assert '"Not sent. ' in script
    style = _style(page)
    for name in ("status-note", "status-ok", "status-fail"):
        declarations = _declarations(style, f".{name}")
        assert "background" in declarations
        assert "border-color" in declarations


def test_send_button_blocked_state_is_exposed_without_relying_on_color() -> None:
    page = _page_direct()
    button = _tag(page, "button", "send-button")
    assert 'aria-disabled="true"' in button
    # The disabled attribute would drop the button out of the tab order, so the
    # reason for the block could never be reached by keyboard.
    assert re.search(r"(?<![-\w])disabled(?![-\w=])", button) is None
    assert 'aria-describedby="send-requirements"' in button
    assert "Check both boxes above" in page
    blocked = _declarations(_style(page), '.button[aria-disabled="true"]')
    assert "dashed" in blocked["border"]
    assert "opacity" not in blocked


def test_send_button_unblocks_only_with_both_confirmations_and_a_record() -> None:
    ready = render_page(_confirmed_state(), _direct(), _FAKE_TOKEN)
    assert 'aria-disabled="false"' in _tag(ready, "button", "send-button")

    state = _confirmed_state()
    state.set_included("m-1", False)
    state.set_included("m-2", False)
    empty = render_page(state, _direct(), _FAKE_TOKEN)
    assert 'aria-disabled="true"' in _tag(empty, "button", "send-button")


def test_report_button_unblocks_only_with_both_confirmations_and_a_record() -> None:
    ready = render_page(_confirmed_state(), _download_only(), _FAKE_TOKEN)
    assert 'aria-disabled="false"' in _tag(ready, "button", "report-button")

    state = _confirmed_state()
    state.set_included("m-1", False)
    state.set_included("m-2", False)
    empty = render_page(state, _download_only(), _FAKE_TOKEN)
    assert 'aria-disabled="true"' in _tag(empty, "button", "report-button")
    assert 'id="report-submission" name="submission" value=""' in empty


def test_script_keeps_report_payload_current_and_prevents_blocked_submit() -> None:
    script = _script(render_page(_state(), _download_only(), _FAKE_TOKEN))
    assert "reportSubmission.value = text" in script
    assert 'reportSubmission.value = ""' in script
    assert "packageReady = false" in script
    assert 'reportForm.addEventListener("submit"' in script
    assert "blocked(reportButton)" in script
    assert "event.preventDefault()" in script
    assert "new Date().toISOString()" in script


def test_download_link_states_its_purpose_and_its_blocked_state() -> None:
    page = _page_direct()
    link = _tag(page, "a", "download-link")
    assert 'download="glite-submission-package.json"' in link
    assert 'aria-disabled="false"' in link
    # No aria-describedby: the note it pointed at ("Downloads the same exact
    # JSON available below") described the link sitting beside it, so the link's
    # own text carried the whole meaning already.
    assert ">Download package</a>" in page

    state = _confirmed_state()
    state.set_included("m-1", False)
    state.set_included("m-2", False)
    blocked = render_page(state, _direct(), _FAKE_TOKEN)
    assert 'aria-disabled="true"' in _tag(blocked, "a", "download-link")


def test_scrollable_package_box_is_named_and_keyboard_reachable() -> None:
    page = _page_direct()
    box = _tag(page, "pre", "package-view")
    assert 'tabindex="0"' in box
    assert 'role="region"' in box
    assert 'aria-labelledby="package-heading"' in box
    assert "overflow-x: auto" in _rule_body(_style(page), "pre")


# --- keyboard ---------------------------------------------------------------


def _focus_order(page: str) -> list[str]:
    names = {
        'class="skip-link"': "skip",
        'class="record-toggle"': "record",
        'class="record-info"': "info",
        'id="action-bar-link"': "action-bar",
        'href="https://glite.ai/policies/privacy"': "privacy-link",
        'href="https://glite.ai/policies/terms-and-conditions"': "terms-link",
        'id="package-heading"': "package-toggle",
        'id="package-view"': "package",
        'id="adult-confirmed"': "adult",
        'id="storage-confirmed"': "storage",
        'id="report-button"': "report",
        'id="download-link"': "download",
        'id="send-button"': "send",
    }
    body = page.split("<body", 1)[1].split("<script>", 1)[0]
    pattern = re.compile(r'<(?:a|button|input|summary)\b[^>]*>|<[a-z]+\b[^>]*\btabindex="0"[^>]*>')
    order: list[str] = []
    for match in pattern.finditer(body):
        tag = match.group(0)
        if 'type="hidden"' in tag:
            continue
        for marker, name in names.items():
            if marker in tag:
                order.append(name)
                break
        else:  # pragma: no cover - a new control must be added to the map
            raise AssertionError(f"unmapped interactive element: {tag}")
    return order


def test_focus_order_runs_records_then_confirmations_then_actions() -> None:
    assert _focus_order(_page_direct()) == [
        "skip",
        "record",
        "info",
        "record",
        "info",
        # The bar sits after the list and before the confirmations, which is
        # where a reader who has finished ticking wants to go next.
        "action-bar",
        "adult",
        "storage",
        "privacy-link",
        "terms-link",
        "report",
        "download",
        "send",
        "package-toggle",
        "package",
    ]
    assert _focus_order(render_page(_state(), _download_only(), _FAKE_TOKEN)) == [
        "skip",
        "record",
        "info",
        "record",
        "info",
        "action-bar",
        "adult",
        "storage",
        "privacy-link",
        "terms-link",
        "report",
        "download",
        "package-toggle",
        "package",
    ]


def test_no_positive_tabindex_overrides_the_document_order() -> None:
    page = _page_direct()
    assert re.search(r'tabindex="[1-9]', page) is None


def test_skip_link_targets_the_send_section_and_shows_itself_on_focus() -> None:
    page = _page_direct()
    assert f'<a class="skip-link" href="#send-section">{SKIP_LINK_TEXT}</a>' in page
    section = _tag(page, "section", "send-section")
    assert 'tabindex="-1"' in section
    focused = _declarations(_style(page), ".skip-link:focus")
    assert focused["left"] == "0.75rem"


def test_focus_is_visible_and_never_removed() -> None:
    style = _style(_page_direct())
    focus = _declarations(style, ":focus-visible")
    assert focus["outline"] == "3px solid var(--focus)"
    assert focus["outline-offset"] == "2px"
    assert "@supports not selector(:focus-visible)" in style
    assert "outline: none" not in style
    assert "outline: 0" not in style


def test_pointer_targets_meet_the_minimum_size() -> None:
    style = _style(_page_direct())
    checkbox = _declarations(style, 'input[type="checkbox"]')
    assert checkbox["width"] == "1.5rem"
    assert checkbox["height"] == "1.5rem"
    button = _declarations(style, ".button")
    assert button["padding"] == "0.55rem 1.1rem"


# --- motion, hover, and print ----------------------------------------------


def test_reduced_motion_is_honored() -> None:
    style = _style(_page_direct())
    start, end = _block_span(style, "@media (prefers-reduced-motion: reduce)")
    block = style[start:end]
    for declaration in (
        "animation-duration: 0.01ms !important",
        "animation-iteration-count: 1 !important",
        "transition-duration: 0.01ms !important",
        "scroll-behavior: auto !important",
    ):
        assert declaration in block


def test_record_details_work_on_hover_focus_click_and_escape() -> None:
    page = _page_direct()
    script = _script(page)
    assert ".record-info:hover" in _style(page)
    assert 'button.addEventListener("mouseenter"' in script
    assert 'button.addEventListener("focus"' in script
    assert 'button.addEventListener("click"' in script
    assert 'event.key === "Escape"' in script
    assert 'panel.addEventListener("mouseenter"' in script
    assert 'document.addEventListener("pointerdown"' in script
    assert "title=" not in page


def test_print_output_keeps_the_package_readable_without_color() -> None:
    style = _style(_page_direct())
    start, end = _block_span(style, "@media print")
    block = style[start:end]
    assert "white-space: pre-wrap" in block
    assert "overflow: visible" in block


# --- narrow widths ----------------------------------------------------------


def test_narrow_width_never_scrolls_the_body_sideways() -> None:
    page = _page_direct()
    style = _style(page)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "box-sizing: border-box" in _rule_body(style, "*")
    body = _declarations(style, "body")
    assert body["max-width"] == "52rem"
    assert "min-width" not in body
    # A fieldset defaults to min-content width, which is what forces a sideways
    # scroll on a phone when a long confirmation sentence is inside it.
    assert _declarations(style, "fieldset.confirmations")["min-width"] == "0"
    assert _declarations(style, "dl.record-fields dd")["overflow-wrap"] == "anywhere"
    assert _declarations(style, "dl.summary dd")["overflow-wrap"] == "anywhere"
    visible_style = style.replace(_rule_body(style, ".visually-hidden"), "")
    assert "white-space: nowrap" not in visible_style
    start, end = _block_span(style, "@media (max-width: 40rem)")
    narrow = style[start:end]
    assert "grid-template-columns: 1fr" in narrow
    assert "width: 100%" in narrow


def test_wide_content_scrolls_inside_its_own_box() -> None:
    style = _style(_page_direct())
    pre = _declarations(style, "pre")
    assert pre["overflow-x"] == "auto"
    assert pre["max-width"] == "100%"
    assert _declarations(style, ".button")["max-width"] == "100%"
    assert _declarations(style, ".actions")["flex-wrap"] == "wrap"


# --- contrast ---------------------------------------------------------------


def test_every_color_is_a_palette_token() -> None:
    """No literal color may escape the two :root blocks, or contrast is unchecked."""
    style = _style(_page_direct())
    remainder = re.sub(r":root\s*\{[^}]*\}", "", style)
    start, end = _block_span(remainder, "@media print")
    remainder = remainder[:start] + remainder[end:]
    literals = re.findall(
        r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{3})(?![0-9A-Za-z_-])",
        remainder,
    )
    assert not literals, f"hard-coded colors outside the palette: {literals}"
    assert not re.findall(r"\brgba?\(", remainder)


def test_palette_meets_wcag_aa_contrast_in_both_themes() -> None:
    palettes = _palettes(_page_direct())
    assert set(palettes) == {"light", "dark"}
    failures: list[str] = []
    for theme, palette in palettes.items():
        page_background = _resolved(palette, "--bg")
        for foreground, background, minimum in (
            *((pair[0], pair[1], 4.5) for pair in _TEXT_PAIRS),
            *((pair[0], pair[1], 3.0) for pair in _NON_TEXT_PAIRS),
        ):
            backdrop = _resolved(palette, background, page_background)
            ratio = _contrast(_resolved(palette, foreground, backdrop), backdrop)
            if ratio + 1e-9 < minimum:
                failures.append(
                    f"{theme}: {foreground} on {background} is {ratio:.2f}:1, needs {minimum}"
                )
    assert not failures, "; ".join(failures)


def test_contrast_helper_matches_known_reference_ratios() -> None:
    """Guards the math itself: black on white is 21:1 and mid gray is 3.95:1."""
    assert round(_contrast((0, 0, 0), (255, 255, 255)), 2) == 21.0
    assert round(_contrast((255, 255, 255), (255, 255, 255)), 2) == 1.0
    assert round(_contrast((119, 119, 119), (255, 255, 255)), 2) == 4.48
    assert _composite((255, 255, 255), 0.5, (0, 0, 0)) == (128, 128, 128)


def test_verification_state_is_never_carried_by_color_alone() -> None:
    page = _page_direct()
    script = _script(page)
    # Every state the page can be in names itself in words.
    for wording in (
        "Sending.",
        "Sent. Submission ID: ",
        "Not sent. ",
        "Nothing to download. Include at least one record.",
    ):
        assert wording in script
    assert "Check both boxes above" in page
    assert 'aria-disabled="true"' in page


def test_a_decision_the_server_refused_is_undone_and_reported() -> None:
    script = _script(_page_direct())
    assert "function revert(box, error)" in script
    assert "box.checked = !box.checked" in script
    assert '"Not saved. The local server refused this change."' in script
    assert script.count("function (error) { revert(box, error); }") == 2
    # A refused decision must not reach applyCounts, or the announced count
    # becomes "undefined" for anyone listening to the live region.
    assert 'if (!response.ok) { throw new Error("decision-refused"); }' in script


def test_static_or_disconnected_page_becomes_read_only() -> None:
    script = _script(_page_direct())
    assert 'window.location.protocol === "file:"' in script
    assert 'token === "STATIC-SNAPSHOT-NO-LIVE-TOKEN"' in script
    assert "setDecisionControlsDisabled(true)" in script
    assert "This saved copy is read-only" in script
    assert "The local review server is no longer available" in script


def test_the_page_is_named_for_the_report_not_the_chore() -> None:
    """The reader came for a report, not to sort a list.

    Title and heading both said "choose", which is the step we need from them,
    while the thing they want was the last heading on the page, under everything
    else. A page named for its chore reads as work with no stated payoff -- and
    every other sentence here describes a cost.
    """
    page = render_page(_state(), _download_only(), _FAKE_TOKEN)
    assert "<h1>Create your English report</h1>" in page
    assert "Create your English report — Glite" in page
    # The h1 owns the goal, so the final section says which step it is.
    assert "Confirm and create" in page
    assert "Choose which mistakes to share" not in page
