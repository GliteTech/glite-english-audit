"""Aider history-file parsing.

Implements the record schemas, inclusion, exclusion, and failure rules from
``specifications/sources/aider.md`` (sections 4, 6, and 9) for the two Aider
channels: the ``prompt_toolkit`` input history (primary, spec 4.1) and the
chat Markdown transcript (fallback, spec 4.2). Timestamps in both files are
naive local time without timezone information; they are kept naive and the
extracted records carry timezone-unknown flags. Only the two ``scan_*``
functions touch the filesystem, and each reads exactly one already-allowlisted
file in a single pass.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

# Spec 6.1: text pasted straight into the terminal prompt is stored like typed
# text; entries above this length carry a possible-paste content flag. No step
# consumes the flag yet; the step-3 authorship skill removes pasted material.
PASTE_LENGTH_THRESHOLD = 2000

# Spec 9: quarantine a file when replacement characters exceed 1% of content.
ENCODING_DEGRADED_RATIO = 0.01

# Spec 9: more than 50% undated entries degrades the timestamp fingerprint.
TIMESTAMP_DEGRADED_RATIO = 0.5

# Spec 4.1: `str(datetime.now())`, fractional part absent when zero.
_TIMESTAMP_LINE = re.compile(r"^# (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*$")
_CHAT_BANNER_PREFIX = "# aider chat started at "
_USER_LINE_PREFIX = "#### "
_FENCE_MARKERS = ("```", "~~~")


def parse_naive_timestamp(value: str) -> datetime | None:
    """Parse ``str(datetime.now())`` output: naive local time (spec 4.1)."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return None if parsed.tzinfo is not None else parsed


def _line_ending_style(text: str) -> str:
    total = text.count("\n")
    if total == 0:
        return "none"
    crlf = text.count("\r\n")
    if crlf == 0:
        return "lf"
    return "crlf" if crlf == total else "mixed"


def _entry_kept_text(raw_text: str) -> str | None:
    """Apply the spec 6.1 inclusion rules; ``None`` means excluded."""
    trimmed = raw_text.strip()
    if not trimmed or trimmed.startswith(("/", "!")):
        return None
    first_line = trimmed.split("\n", 1)[0].strip()
    if first_line == "{":
        # Legacy brace multiline-block opener (spec 6.1 rule 4).
        return None
    return trimmed


@dataclass(frozen=True)
class KeptEntry:
    """One input-history entry that passed the spec 6.1 inclusion rules."""

    ordinal: int
    text: str
    timestamp: datetime | None
    content_flags: tuple[str, ...]


@dataclass
class InputHistoryScan:
    """Aggregate result of scanning one ``.aider.input.history`` file."""

    file_name: str
    line_ending: str = "none"
    entry_count: int = 0
    undated_entries: int = 0
    timestamp_parse_failures: int = 0
    replacement_characters: int = 0
    total_characters: int = 0
    has_content: bool = False
    truncated_tail: bool = False
    kept: list[KeptEntry] = field(default_factory=list)

    @property
    def encoding_degraded(self) -> bool:
        """Spec 9: over-threshold replacement characters quarantine the file."""
        return (
            self.total_characters > 0
            and self.replacement_characters > self.total_characters * ENCODING_DEGRADED_RATIO
        )

    @property
    def timestamp_degraded(self) -> bool:
        """Spec 9: more than half of the entries lack a parsable timestamp."""
        return (
            self.entry_count > 0
            and self.undated_entries > self.entry_count * TIMESTAMP_DEGRADED_RATIO
        )

    @property
    def unsupported(self) -> bool:
        """Spec 9: mojibake, or non-empty content without any entry line."""
        return self.encoding_degraded or (self.has_content and self.entry_count == 0)

    @property
    def earliest_kept_timestamp(self) -> datetime | None:
        stamps = [entry.timestamp for entry in self.kept if entry.timestamp is not None]
        return min(stamps) if stamps else None


def scan_input_history(path: Path) -> InputHistoryScan:
    """Stream-classify one input-history file (spec 4.1 read-back rules).

    Any line starting with ``+`` continues the current entry; any other line
    terminates it. Each entry is associated with the nearest preceding
    parsable ``# `` timestamp line. A trailing ``+`` block without a final
    newline is an interrupted live append and is dropped whole (spec 8.2).
    """
    scan = InputHistoryScan(file_name=path.name)
    text = path.read_bytes().decode("utf-8", errors="replace")
    scan.total_characters = len(text)
    scan.replacement_characters = text.count("\ufffd")
    scan.line_ending = _line_ending_style(text)
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    current: list[str] | None = None
    current_timestamp: datetime | None = None
    last_timestamp: datetime | None = None

    def count_entry() -> None:
        scan.entry_count += 1
        if current_timestamp is None:
            scan.undated_entries += 1

    def finalize() -> None:
        nonlocal current
        if current is None:
            return
        count_entry()
        kept_text = _entry_kept_text("\n".join(current))
        current = None
        if kept_text is None:
            return
        flags = ["timezone_unknown"] if current_timestamp is not None else ["undated"]
        if len(kept_text) > PASTE_LENGTH_THRESHOLD:
            flags.append("possible_paste")
        scan.kept.append(
            KeptEntry(
                ordinal=scan.entry_count - 1,
                text=kept_text,
                timestamp=current_timestamp,
                content_flags=tuple(flags),
            )
        )

    last_index = len(lines) - 1
    for index, raw_line in enumerate(lines):
        line = raw_line.removesuffix("\r")
        if line.startswith("+"):
            if current is None:
                current = []
                current_timestamp = last_timestamp
            current.append(line[1:])
            if index == last_index and not ends_with_newline:
                # Interrupted append mid-entry: drop the whole entry.
                scan.truncated_tail = True
                count_entry()
                current = None
            continue
        finalize()
        if not line.strip():
            continue
        scan.has_content = True
        match = _TIMESTAMP_LINE.match(line)
        if match is not None:
            last_timestamp = parse_naive_timestamp(match.group(1))
            if last_timestamp is None:
                scan.timestamp_parse_failures += 1
        elif line.startswith("#"):
            # A comment line that is not a parsable timestamp leaves the
            # entries that follow it undated (spec 9).
            scan.timestamp_parse_failures += 1
            last_timestamp = None
    finalize()
    if scan.truncated_tail and scan.kept:
        last = scan.kept[-1]
        scan.kept[-1] = replace(last, content_flags=(*last.content_flags, "truncated_tail_dropped"))
    return scan


@dataclass(frozen=True)
class KeptChatMessage:
    """One chat-markdown user message that passed the spec 6.2 rules."""

    banner_ordinal: int
    message_ordinal: int
    text: str
    timestamp: datetime | None
    content_flags: tuple[str, ...]


@dataclass
class ChatMarkdownScan:
    """Aggregate result of scanning one ``.aider.chat.history.md`` file."""

    file_name: str
    line_ending: str = "none"
    banner_count: int = 0
    user_prefix_lines: int = 0
    formed_messages: int = 0
    timestamp_parse_failures: int = 0
    replacement_characters: int = 0
    total_characters: int = 0
    has_content: bool = False
    truncated_tail: bool = False
    unbalanced_fence: bool = False
    kept: list[KeptChatMessage] = field(default_factory=list)

    @property
    def encoding_degraded(self) -> bool:
        """Spec 9: over-threshold replacement characters quarantine the file."""
        return (
            self.total_characters > 0
            and self.replacement_characters > self.total_characters * ENCODING_DEGRADED_RATIO
        )

    @property
    def unsupported(self) -> bool:
        """Spec 9: mojibake, or user-prefix lines that never form a message."""
        return self.encoding_degraded or (self.user_prefix_lines > 0 and self.formed_messages == 0)

    @property
    def earliest_kept_timestamp(self) -> datetime | None:
        stamps = [message.timestamp for message in self.kept if message.timestamp is not None]
        return min(stamps) if stamps else None


def scan_chat_markdown(path: Path) -> ChatMarkdownScan:
    """Stream-classify one chat Markdown transcript (spec 6.2 state machine).

    Fenced code blocks are tracked so a ``#### `` line inside a fence is never
    user text. Consecutive ``#### `` lines outside fences form one message,
    attributed to the enclosing ``# aider chat started`` banner. CRLF line
    endings are accepted unconditionally (spec 10.6).
    """
    scan = ChatMarkdownScan(file_name=path.name)
    text = path.read_bytes().decode("utf-8", errors="replace")
    scan.total_characters = len(text)
    scan.replacement_characters = text.count("\ufffd")
    scan.line_ending = _line_ending_style(text)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif lines:
        # Spec 8.2: an unterminated final line is an interrupted live append.
        scan.truncated_tail = True
        lines.pop()

    in_fence = False
    banner_ordinal = 0
    banner_timestamp: datetime | None = None
    message_ordinal = 0
    current: list[str] | None = None

    def finalize() -> None:
        nonlocal current, message_ordinal
        if current is None:
            return
        scan.formed_messages += 1
        ordinal = message_ordinal
        message_ordinal += 1
        trimmed = "\n".join(current).strip()
        current = None
        if not trimmed or trimmed == "<blank>" or trimmed.startswith(("/", "!")):
            return
        flags = ["fallback_channel"]
        flags.append("session_start_time_only" if banner_timestamp is not None else "undated")
        if len(trimmed) > PASTE_LENGTH_THRESHOLD:
            flags.append("possible_paste")
        scan.kept.append(
            KeptChatMessage(
                banner_ordinal=banner_ordinal,
                message_ordinal=ordinal,
                text=trimmed,
                timestamp=banner_timestamp,
                content_flags=tuple(flags),
            )
        )

    for raw_line in lines:
        line = raw_line.removesuffix("\r")
        if line.strip():
            scan.has_content = True
        if line.startswith(_USER_LINE_PREFIX):
            scan.user_prefix_lines += 1
        if line.lstrip().startswith(_FENCE_MARKERS):
            finalize()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(_CHAT_BANNER_PREFIX):
            finalize()
            banner_ordinal += 1
            message_ordinal = 0
            scan.banner_count += 1
            banner_timestamp = parse_naive_timestamp(line[len(_CHAT_BANNER_PREFIX) :])
            if banner_timestamp is None:
                scan.timestamp_parse_failures += 1
            continue
        if line.startswith(_USER_LINE_PREFIX):
            if current is None:
                current = []
            current.append(line[len(_USER_LINE_PREFIX) :])
            continue
        finalize()
    if in_fence:
        scan.unbalanced_fence = True
    finalize()
    return scan
