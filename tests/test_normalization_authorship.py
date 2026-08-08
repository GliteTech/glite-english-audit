"""Line-level removal of non-authored material (specification, 4.5)."""

import time

from glite_english_audit.normalization.authorship import (
    FLAG_BLOCKQUOTE,
    FLAG_CODE_FENCE,
    FLAG_INDENTED_CODE,
    FLAG_LOG_LINE,
    FLAG_MARKUP_LINE,
    FLAG_SYMBOL_LINE,
    FLAG_URL_LINE,
    PRODUCER_VERSION,
    strip_non_authored,
)


def test_producer_version_is_semver() -> None:
    parts = PRODUCER_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_plain_prose_untouched() -> None:
    text = "I think we should retry the release.\nIt failed twice yesterday."
    result = strip_non_authored(text)
    assert result.cleaned_text == text
    assert result.removed_flags == []
    assert result.removed_char_count == 0


def test_fenced_code_block_removed() -> None:
    text = "Please check this:\n```python\nprint('hello')\n```\nDoes it look right?"
    result = strip_non_authored(text)
    assert result.cleaned_text == "Please check this:\nDoes it look right?"
    assert FLAG_CODE_FENCE in result.removed_flags
    assert result.removed_char_count == len("```python") + len("print('hello')") + len("```")


def test_tilde_fence_removed() -> None:
    text = "before\n~~~\nsome code\n~~~\nafter"
    result = strip_non_authored(text)
    assert result.cleaned_text == "before\nafter"
    assert FLAG_CODE_FENCE in result.removed_flags


def test_unclosed_fence_removed_to_end() -> None:
    text = "my question is why\n```\ncode line one\ncode line two"
    result = strip_non_authored(text)
    assert result.cleaned_text == "my question is why"
    assert FLAG_CODE_FENCE in result.removed_flags


def test_blockquote_lines_removed() -> None:
    text = "You wrote earlier:\n> the build is broken\nand I agree with that."
    result = strip_non_authored(text)
    assert result.cleaned_text == "You wrote earlier:\nand I agree with that."
    assert result.removed_flags == [FLAG_BLOCKQUOTE]


def test_traceback_and_file_lines_removed() -> None:
    text = (
        "here is the error I got\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 12, in main\n'
        "why does it happen?"
    )
    result = strip_non_authored(text)
    assert result.cleaned_text == "here is the error I got\nwhy does it happen?"
    assert result.removed_flags == [FLAG_LOG_LINE]


def test_stack_frame_timestamp_and_severity_lines_removed() -> None:
    text = (
        "the console shows\n"
        "at Object.render (app.js:10:5)\n"
        "[12:34:56] server restarted\n"
        "2026-01-05 09:15:00 request finished\n"
        "ERROR: connection refused\n"
        "so I am stuck"
    )
    result = strip_non_authored(text)
    assert result.cleaned_text == "the console shows\nso I am stuck"
    assert result.removed_flags == [FLAG_LOG_LINE]


def test_prose_starting_with_at_kept() -> None:
    text = "at some point we should refactor this module"
    result = strip_non_authored(text)
    assert result.cleaned_text == text


def test_indented_code_removed_indented_prose_kept() -> None:
    text = (
        "compare these two lines\n"
        "    result = compute(x)\n"
        "    just an indented sentence without code\n"
        "which one is right?"
    )
    result = strip_non_authored(text)
    assert result.cleaned_text == (
        "compare these two lines\n    just an indented sentence without code\nwhich one is right?"
    )
    assert result.removed_flags == [FLAG_INDENTED_CODE]


def test_json_and_xml_lines_removed() -> None:
    text = (
        "the config looks like\n"
        "{\n"
        '"retries": 3,\n'
        "}\n"
        "<configuration enabled='true'>\n"
        "is that valid?"
    )
    result = strip_non_authored(text)
    assert result.cleaned_text == "the config looks like\nis that valid?"
    assert result.removed_flags == [FLAG_MARKUP_LINE]


def test_url_only_line_removed_url_in_sentence_kept() -> None:
    text = "the docs at https://example.com/setup explain it\nhttps://example.com/other"
    result = strip_non_authored(text)
    assert result.cleaned_text == "the docs at https://example.com/setup explain it"
    assert result.removed_flags == [FLAG_URL_LINE]


def test_mostly_symbol_line_removed() -> None:
    text = "section one\n=====================\nsection two"
    result = strip_non_authored(text)
    assert result.cleaned_text == "section one\nsection two"
    assert result.removed_flags == [FLAG_SYMBOL_LINE]


def test_half_letters_line_kept() -> None:
    # Exactly half of the visible characters are letters: kept.
    result = strip_non_authored("ab !?")
    assert result.cleaned_text == "ab !?"
    assert result.removed_flags == []


def test_blank_lines_kept() -> None:
    text = "first paragraph\n\nsecond paragraph"
    result = strip_non_authored(text)
    assert result.cleaned_text == text


def test_order_preserved_and_flags_first_hit_order() -> None:
    text = "> quoted\none\nERROR: boom\ntwo\n> quoted again\nthree"
    result = strip_non_authored(text)
    assert result.cleaned_text == "one\ntwo\nthree"
    assert result.removed_flags == [FLAG_BLOCKQUOTE, FLAG_LOG_LINE]


def test_removed_char_count_excludes_line_terminators() -> None:
    text = "keep me\n> drop me\nkeep me too"
    result = strip_non_authored(text)
    assert result.removed_char_count == len("> drop me")


def test_deterministic() -> None:
    text = "prose\n```\ncode\n```\n> quote\nmore prose"
    assert strip_non_authored(text) == strip_non_authored(text)


def test_long_input_runs_fast() -> None:
    # No catastrophic backtracking: a large mixed input finishes quickly.
    chunk = (
        "this is an ordinary authored sentence about the build\n"
        "> quoted reply from the agent\n"
        "[12:34:56] INFO: something happened\n"
        '"key": "value",\n'
        "    indented = code_line(1)\n"
    )
    text = chunk * 20_000
    start = time.monotonic()
    result = strip_non_authored(text)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    lines = result.cleaned_text.splitlines()
    assert len(lines) == 20_000
    assert all(line.startswith("this is an ordinary") for line in lines)
