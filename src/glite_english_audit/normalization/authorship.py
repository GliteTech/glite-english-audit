"""Removal of non-authored material from candidate text (specification, 4.5).

Adapters attribute whole records structurally; this layer removes pasted,
quoted, generated, and code-like lines *inside* an otherwise user-authored
record. The rules are line-based, deterministic, and order-preserving:

- Fenced code blocks (``` or ~~~ fences, up to 3 leading spaces), including
  an unclosed fence running to the end of the text.
- Blockquote lines starting with ``>``.
- Log and stack-trace lines: leading ``Traceback``, JS/Java-style
  ``at frame(...)`` lines, Python ``File "...", line N`` lines, leading
  ``[HH:MM:SS]``/``HH:MM:SS`` or ISO date-time stamps, and uppercase
  severity prefixes ``ERROR:``/``WARNING:``/``WARN:``/``INFO:``/``DEBUG:``.
- Indented code: lines indented 4+ spaces (or a tab) that also carry a code
  signal — a character from ``{}()[];=<>|`$``, a trailing ``:``, or an
  unambiguous keyword opener (``def``, ``return``, ``import``, ``from``,
  ``class``, ``const``, ``let``, ``var``, ``function``). Indented prose
  without a code signal is kept.
- JSON/XML-looking lines: ``<...>`` lines, ``"key":`` lines, and lines that
  both start and end with JSON structure characters.
- Lines whose non-whitespace characters are more than 50% non-letters.
- Lines consisting only of URL tokens. URLs inside a sentence are kept; the
  tokenizer already excludes them from the word count.

Whole-utterance rejection is the caller's policy; this module only cleans
and flags. Every pattern is a simple anchored expression with no nested
quantifiers, so runtime stays linear in input size.
"""

import re

from pydantic import BaseModel, ConfigDict, Field

PRODUCER_VERSION = "1.0.0"

FLAG_CODE_FENCE = "code_fence"
FLAG_INDENTED_CODE = "indented_code"
FLAG_BLOCKQUOTE = "blockquote"
FLAG_LOG_LINE = "log_line"
FLAG_MARKUP_LINE = "markup_line"
FLAG_SYMBOL_LINE = "symbol_line"
FLAG_URL_LINE = "url_line"

_FENCE_OPEN = re.compile(r"^ {0,3}(```|~~~)")
_TRACEBACK = re.compile(r"^Traceback\b")
_STACK_FRAME = re.compile(r"^at \S+ ?\(.*\)$")
_PY_TRACE_FILE = re.compile(r'^File ".*", line \d+')
_TIME_PREFIX = re.compile(r"^\[?\d{1,2}:\d{2}:\d{2}")
_DATETIME_PREFIX = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_SEVERITY_PREFIX = re.compile(r"^(?:ERROR|WARNING|WARN|INFO|DEBUG):")
_JSON_KEY = re.compile(r'^"[^"\n]*"\s*:')
_URL_TOKEN = re.compile(r"://|^www\.", re.IGNORECASE)

_CODE_HINT_CHARS = frozenset("{}()[];=<>|`$")
_CODE_KEYWORD_OPENERS = (
    "def ",
    "return ",
    "return;",
    "import ",
    "from ",
    "class ",
    "const ",
    "let ",
    "var ",
    "function ",
)


class AuthorshipResult(BaseModel):
    """Cleaned text plus what was removed, without the removed content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cleaned_text: str
    removed_flags: list[str] = Field(default_factory=list)
    removed_char_count: int = Field(ge=0)


def _is_log_line(stripped: str) -> bool:
    return bool(
        _TRACEBACK.match(stripped)
        or _STACK_FRAME.match(stripped)
        or _PY_TRACE_FILE.match(stripped)
        or _TIME_PREFIX.match(stripped)
        or _DATETIME_PREFIX.match(stripped)
        or _SEVERITY_PREFIX.match(stripped)
    )


def _is_indented_code(line: str, stripped: str) -> bool:
    if not (line.startswith("    ") or line.startswith("\t")):
        return False
    if any(ch in _CODE_HINT_CHARS for ch in stripped):
        return True
    return stripped.endswith(":") or stripped.startswith(_CODE_KEYWORD_OPENERS)


def _is_url_only(stripped: str) -> bool:
    tokens = stripped.split()
    return all(_URL_TOKEN.search(token) for token in tokens)


def _is_markup_line(stripped: str) -> bool:
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    if _JSON_KEY.match(stripped):
        return True
    if stripped[0] in "{}[]":
        return len(stripped) == 1 or stripped[-1] in '{}[],:"'
    return False


def _is_mostly_symbols(stripped: str) -> bool:
    visible = [ch for ch in stripped if not ch.isspace()]
    letter_count = sum(1 for ch in visible if ch.isalpha())
    # Strictly more than 50% non-letters; an exact half stays.
    return letter_count * 2 < len(visible)


def _removal_flag(line: str, stripped: str) -> str | None:
    if not stripped:
        return None
    if stripped.startswith(">"):
        return FLAG_BLOCKQUOTE
    if _is_log_line(stripped):
        return FLAG_LOG_LINE
    if _is_indented_code(line, stripped):
        return FLAG_INDENTED_CODE
    if _is_url_only(stripped):
        return FLAG_URL_LINE
    if _is_markup_line(stripped):
        return FLAG_MARKUP_LINE
    if _is_mostly_symbols(stripped):
        return FLAG_SYMBOL_LINE
    return None


def _record(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


def strip_non_authored(text: str) -> AuthorshipResult:
    """Remove non-authored lines from ``text``, keeping authored lines in order.

    ``cleaned_text`` joins kept lines with ``\\n``; line terminators are
    normalized and excluded from ``removed_char_count``, which counts only
    the characters of removed lines. ``removed_flags`` lists each triggered
    rule once, in first-hit order.
    """
    kept_lines: list[str] = []
    flags: list[str] = []
    removed_chars = 0
    in_fence = False
    fence_marker = ""

    for line in text.splitlines():
        stripped = line.strip()
        if in_fence:
            removed_chars += len(line)
            if stripped.startswith(fence_marker):
                in_fence = False
            continue

        fence = _FENCE_OPEN.match(line)
        if fence:
            in_fence = True
            fence_marker = fence.group(1)
            removed_chars += len(line)
            _record(flags, FLAG_CODE_FENCE)
            continue

        flag = _removal_flag(line, stripped)
        if flag is None:
            kept_lines.append(line)
        else:
            removed_chars += len(line)
            _record(flags, flag)

    return AuthorshipResult(
        cleaned_text="\n".join(kept_lines),
        removed_flags=flags,
        removed_char_count=removed_chars,
    )
