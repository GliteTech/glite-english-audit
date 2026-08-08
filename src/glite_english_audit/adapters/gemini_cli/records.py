"""Gemini CLI session-record classification.

Implements the generation detection, inclusion, exclusion, wrapper, and
failure rules from ``specifications/sources/gemini_cli.md`` (sections 3, 5,
6, and 9). Both session generations are feature-detected per file; no parsing
decision branches on a CLI version number. Only the scan functions touch the
filesystem, and each reads exactly one already-allowlisted chat file.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Spec 7.4: monolithic J1 files above this cap are counted as oversize and
# never read into memory.
J1_SIZE_CAP_BYTES = 8 * 1024 * 1024

GENERATION_JSONL = "jsonl-v1"
GENERATION_JSON = "json-v1"

# Spec 5.2.2: one such part anywhere in ``content`` poisons the whole record.
_TOOL_PART_KEYS = ("functionCall", "functionResponse", "executableCode", "codeExecutionResult")

# Spec 5.2.4: media parts are ignored silently.
_MEDIA_PART_KEYS = ("inlineData", "fileData")

_KNOWN_MESSAGE_TYPES = frozenset({"user", "gemini", "info", "error", "warning"})
_NOTICE_TYPES = frozenset({"info", "error", "warning"})

# Spec 5.2.7: leading tags of known injected wrappers exclude the record;
# unknown leading tags keep the text with an ``unknown_wrapper`` flag instead.
KNOWN_WRAPPER_TAGS: frozenset[str] = frozenset(
    {
        "session_context",
        "environment_context",
        "system_context",
        "compressed_chat_history",
        "state_snapshot",
    }
)

# Spec 5.2.7 / E13: opening sentence of the injected environment message.
ENVIRONMENT_CONTEXT_SENTENCE = "This is the Gemini CLI. We are setting up the context"

# Spec 5.2.5 / E18: at-command referenced-files marker, feature-detected on a
# single line (two or more leading dashes plus a known marker phrase).
_REFERENCE_MARKER = re.compile(r"^-{2,}.*(?:referenced files|content from)", re.IGNORECASE)

_LEADING_TAG = re.compile(r"^<([A-Za-z][A-Za-z0-9_-]*)>")


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO 8601 record timestamp; unparsable values become None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class LineKind(Enum):
    """Structural kind of one J2 line, in the vendor reader's order (spec 3.1)."""

    REWIND = "rewind"
    SET_UPDATE = "set_update"
    MESSAGE = "message"
    METADATA = "metadata"
    UNKNOWN = "unknown"


def discriminate_line(parsed: object) -> LineKind:
    """Discriminate one parsed J2 line exactly as the vendor reader does."""
    if not isinstance(parsed, dict):
        return LineKind.UNKNOWN
    if isinstance(parsed.get("$rewindTo"), str):
        return LineKind.REWIND
    if isinstance(parsed.get("$set"), dict):
        return LineKind.SET_UPDATE
    if isinstance(parsed.get("id"), str):
        return LineKind.MESSAGE
    if isinstance(parsed.get("sessionId"), str) and isinstance(parsed.get("projectHash"), str):
        return LineKind.METADATA
    return LineKind.UNKNOWN


class MessageOutcome(Enum):
    """Classification of one message record."""

    KEPT = "kept"
    EXCLUDED = "excluded"
    UNKNOWN_TYPE = "unknown_type"
    UNRECOGNIZED_USER = "unrecognized_user"


@dataclass(frozen=True)
class ClassifiedMessage:
    """One message record's classification plus scan-level feature markers."""

    outcome: MessageOutcome
    message_id: str | None = None
    text: str | None = None
    content_flags: tuple[str, ...] = ()
    timestamp: datetime | None = None
    display_used: bool = False
    has_display_content: bool = False
    notice_type: bool = False


def _as_parts(value: object) -> list[str | dict[str, Any]] | None:
    """Normalize a ``PartListUnion``; None when the shape is not supported."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        parts: list[str | dict[str, Any]] = []
        for item in value:
            if not isinstance(item, str | dict):
                return None
            parts.append(item)
        return parts
    return None


def _text_items(parts: list[str | dict[str, Any]], flags: set[str]) -> list[str]:
    """Spec 5.2.4: string items and ``text`` fields, in order; media ignored."""
    items: list[str] = []
    for part in parts:
        if isinstance(part, str):
            items.append(part)
            continue
        if part.get("thought"):
            continue
        text_value = part.get("text")
        if isinstance(text_value, str):
            items.append(text_value)
            continue
        if any(key in part for key in _MEDIA_PART_KEYS):
            continue
        flags.add("unknown_part")
    return items


def _cut_reference_expansion(items: list[str]) -> tuple[list[str], bool]:
    """Spec 5.2.5: drop everything from the first referenced-files marker line."""
    kept: list[str] = []
    for item in items:
        lines = item.split("\n")
        marker_index = next(
            (index for index, line in enumerate(lines) if _REFERENCE_MARKER.match(line.strip())),
            None,
        )
        if marker_index is None:
            kept.append(item)
            continue
        if marker_index > 0:
            kept.append("\n".join(lines[:marker_index]))
        return kept, True
    return kept, False


def classify_message(record: dict[str, Any]) -> ClassifiedMessage:
    """Classify one message record against the spec section 5 rules."""
    raw_id = record.get("id")
    message_id = raw_id if isinstance(raw_id, str) else None
    timestamp = parse_timestamp(record.get("timestamp"))
    has_display = "displayContent" in record

    def make(
        outcome: MessageOutcome,
        *,
        text: str | None = None,
        content_flags: tuple[str, ...] = (),
        display_used: bool = False,
        notice: bool = False,
    ) -> ClassifiedMessage:
        return ClassifiedMessage(
            outcome=outcome,
            message_id=message_id,
            text=text,
            content_flags=content_flags,
            timestamp=timestamp,
            display_used=display_used,
            has_display_content=has_display,
            notice_type=notice,
        )

    message_type = record.get("type")
    if not isinstance(message_type, str) or message_type not in _KNOWN_MESSAGE_TYPES:
        return make(MessageOutcome.UNKNOWN_TYPE)
    if message_type != "user":
        return make(MessageOutcome.EXCLUDED, notice=message_type in _NOTICE_TYPES)
    if message_id is None:
        return make(MessageOutcome.UNRECOGNIZED_USER)

    content_parts = _as_parts(record.get("content"))
    if content_parts is None:
        return make(MessageOutcome.UNRECOGNIZED_USER)
    for part in content_parts:
        if isinstance(part, dict) and any(key in part for key in _TOOL_PART_KEYS):
            return make(MessageOutcome.EXCLUDED)

    flags: set[str] = set()
    source_parts = content_parts
    display_used = False
    if has_display:
        display_parts = _as_parts(record.get("displayContent"))
        if display_parts is None:
            flags.add("unknown_part")
        else:
            source_parts = display_parts
            display_used = True
            flags.add("expanded_content_present")

    items = _text_items(source_parts, flags)
    if not display_used:
        items, trimmed = _cut_reference_expansion(items)
        if trimmed:
            flags.add("reference_expansion_trimmed")
        elif len(items) > 1:
            # Spec 10.3: suspected expansion without a detectable marker.
            flags.add("multipart_no_display")

    text = "\n".join(item.strip() for item in items if item.strip()).strip()
    if not text:
        return make(MessageOutcome.EXCLUDED, content_flags=tuple(sorted(flags)))
    if text.startswith(("/", "!")):
        return make(MessageOutcome.EXCLUDED)
    if text.startswith("<session_context>") or text.startswith(ENVIRONMENT_CONTEXT_SENTENCE):
        return make(MessageOutcome.EXCLUDED)
    tag_match = _LEADING_TAG.match(text)
    if tag_match is not None:
        if tag_match.group(1) in KNOWN_WRAPPER_TAGS:
            return make(MessageOutcome.EXCLUDED)
        flags.add("unknown_wrapper")
    return make(
        MessageOutcome.KEPT,
        text=text,
        content_flags=tuple(sorted(flags)),
        display_used=display_used,
    )


def text_starts_with_injected_marker(text: str) -> bool:
    """Verification guard: True when kept text starts like excluded content."""
    stripped = text.lstrip()
    if stripped.startswith(("/", "!")):
        return True
    if stripped.startswith("<session_context>") or stripped.startswith(
        ENVIRONMENT_CONTEXT_SENTENCE
    ):
        return True
    tag_match = _LEADING_TAG.match(stripped)
    return tag_match is not None and tag_match.group(1) in KNOWN_WRAPPER_TAGS


@dataclass(frozen=True)
class KeptMessage:
    """One candidate user-authored utterance found in a session file."""

    message_id: str
    text: str
    content_flags: tuple[str, ...]
    timestamp: datetime | None
    display_used: bool


@dataclass
class SessionScan:
    """Aggregate result of scanning one chat file."""

    file_name: str
    generation: str | None = None
    session_id: str | None = None
    start_time: datetime | None = None
    subagent: bool = False
    session_meta_missing: bool = False
    non_empty_lines: int = 0
    known_kind_lines: int = 0
    message_records: int = 0
    malformed_lines: int = 0
    skipped_unknown_lines: int = 0
    unknown_message_types: int = 0
    unrecognized_user_records: int = 0
    truncated_tail: bool = False
    oversize_skipped: bool = False
    malformed_file: bool = False
    unsupported_shape: bool = False
    has_display_content: bool = False
    has_notice_types: bool = False
    has_kind_metadata: bool = False
    kept: list[KeptMessage] = field(default_factory=list)

    @property
    def unsupported(self) -> bool:
        """Spec 9: fail closed on unknown shapes and over-threshold damage."""
        if self.unsupported_shape:
            return True
        if self.malformed_lines and self.malformed_lines * 10 > self.non_empty_lines:
            return True
        if self.skipped_unknown_lines and self.known_kind_lines == 0:
            return True
        return bool(self.message_records) and self.unknown_message_types * 2 > (
            self.message_records
        )

    @property
    def extractable(self) -> bool:
        """Whether this file may contribute candidate text at all."""
        return not (self.unsupported or self.malformed_file or self.oversize_skipped)

    @property
    def session_identity(self) -> str | None:
        """Spec 6.1: metadata session ID, else the filename-fragment fallback."""
        if self.session_id is not None:
            return self.session_id
        if self.message_records:
            return Path(self.file_name).stem
        return None


class _MessageCollector:
    """Per-file message accumulator; a repeated ID's last occurrence wins."""

    def __init__(self) -> None:
        self._order: list[str] = []
        self._latest: dict[str, ClassifiedMessage] = {}

    def note(self, scan: SessionScan, classified: ClassifiedMessage) -> None:
        scan.message_records += 1
        if classified.has_display_content:
            scan.has_display_content = True
        if classified.notice_type:
            scan.has_notice_types = True
        if classified.outcome is MessageOutcome.UNKNOWN_TYPE:
            scan.unknown_message_types += 1
            return
        if classified.outcome is MessageOutcome.UNRECOGNIZED_USER:
            scan.unrecognized_user_records += 1
            return
        if classified.message_id is None:
            return
        if classified.message_id not in self._latest:
            self._order.append(classified.message_id)
        # Spec 6.3: J2 append semantics may rewrite a message in place.
        self._latest[classified.message_id] = classified

    def finalize(self, scan: SessionScan) -> None:
        for message_id in self._order:
            classified = self._latest[message_id]
            if classified.outcome is not MessageOutcome.KEPT or classified.text is None:
                continue
            scan.kept.append(
                KeptMessage(
                    message_id=message_id,
                    text=classified.text,
                    content_flags=classified.content_flags,
                    timestamp=classified.timestamp,
                    display_used=classified.display_used,
                )
            )
        # Spec 5.1: a subagent session contributes no candidate text.
        if scan.subagent:
            scan.kept.clear()


def _apply_metadata(scan: SessionScan, record: dict[str, Any]) -> None:
    if scan.session_id is None:
        session_id = record.get("sessionId")
        scan.session_id = session_id if isinstance(session_id, str) else None
    if scan.start_time is None:
        scan.start_time = parse_timestamp(record.get("startTime"))
    if "kind" in record:
        scan.has_kind_metadata = True
        if record.get("kind") == "subagent":
            scan.subagent = True


def _apply_set_update(scan: SessionScan, update: dict[str, Any]) -> None:
    """Read only session identity and kind from a ``$set`` update; never text."""
    session_id = update.get("sessionId")
    if isinstance(session_id, str):
        scan.session_id = session_id
    if "kind" in update:
        scan.has_kind_metadata = True
        if update.get("kind") == "subagent":
            scan.subagent = True


def scan_jsonl_session(path: Path) -> SessionScan:
    """Stream-classify one J2 session file (spec 3.1, 7.4).

    A final line that fails to parse is a live append in progress and is
    dropped silently with a ``truncated_tail`` note; mid-file failures count
    toward the 10% malformed threshold.
    """
    scan = SessionScan(file_name=path.name)
    collector = _MessageCollector()
    bad_positions: list[int] = []
    first_parsed_kind: LineKind | None = None
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            position = scan.non_empty_lines
            scan.non_empty_lines += 1
            try:
                parsed = json.loads(line)
            except ValueError:
                bad_positions.append(position)
                continue
            kind = discriminate_line(parsed)
            if first_parsed_kind is None:
                first_parsed_kind = kind
            if kind is LineKind.UNKNOWN:
                scan.skipped_unknown_lines += 1
                continue
            scan.known_kind_lines += 1
            if kind is LineKind.REWIND:
                continue
            if kind is LineKind.SET_UPDATE:
                _apply_set_update(scan, parsed["$set"])
                continue
            if kind is LineKind.METADATA:
                _apply_metadata(scan, parsed)
                continue
            collector.note(scan, classify_message(parsed))
    for position in bad_positions:
        if position == scan.non_empty_lines - 1:
            scan.truncated_tail = True
        else:
            scan.malformed_lines += 1
    if first_parsed_kind is not None and first_parsed_kind is not LineKind.METADATA:
        scan.session_meta_missing = True
    if scan.known_kind_lines:
        scan.generation = GENERATION_JSONL
    collector.finalize(scan)
    return scan


def scan_json_session(path: Path, *, size_cap_bytes: int = J1_SIZE_CAP_BYTES) -> SessionScan:
    """Parse one monolithic J1 session file whole, bounded by a size cap."""
    scan = SessionScan(file_name=path.name)
    if path.stat().st_size > size_cap_bytes:
        scan.oversize_skipped = True
        return scan
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        scan.malformed_file = True
        return scan
    if not isinstance(payload, dict):
        scan.unsupported_shape = True
        return scan
    session_id = payload.get("sessionId")
    messages = payload.get("messages")
    if not isinstance(session_id, str) or not isinstance(messages, list):
        scan.unsupported_shape = True
        return scan
    scan.generation = GENERATION_JSON
    scan.session_id = session_id
    scan.start_time = parse_timestamp(payload.get("startTime"))
    if "kind" in payload:
        scan.has_kind_metadata = True
        if payload.get("kind") == "subagent":
            scan.subagent = True
    collector = _MessageCollector()
    for entry in messages:
        if not isinstance(entry, dict):
            scan.skipped_unknown_lines += 1
            continue
        collector.note(scan, classify_message(entry))
    collector.finalize(scan)
    return scan


def scan_chat_file(path: Path) -> SessionScan:
    """Scan one allowlisted chat file, dispatching on its extension."""
    if path.suffix == ".jsonl":
        return scan_jsonl_session(path)
    return scan_json_session(path)
