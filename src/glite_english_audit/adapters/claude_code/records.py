"""Claude Code transcript record classification.

Implements the record schema, inclusion, exclusion, wrapper, and failure
rules from ``specifications/sources/claude_code.md`` (sections 3, 4, and 7).
Everything here is feature-detected per record; nothing branches on the
``version`` field alone. Only :func:`scan_transcript` touches the
filesystem, and it reads exactly one already-allowlisted JSONL file.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Spec 4.2: pasted text is user-transferred, not user-authored. Prompts longer
# than this carry a possible-paste content flag. No step consumes the flag
# yet; the step-3 authorship skill is what currently removes pasted material.
PASTE_LENGTH_THRESHOLD = 2000

KNOWN_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "user",
        "assistant",
        "system",
        "attachment",
        "summary",
        "ai-title",
        "last-prompt",
        "queue-operation",
        "mode",
        "permission-mode",
        "file-history-snapshot",
        "file-history-delta",
        "agent-name",
        "pr-link",
        "relocated",
        "worktree-state",
        "bridge-session",
        "frame-link",
    }
)

# Types that only one storage generation writes; used for the fingerprint.
_CURRENT_ONLY_TYPES = frozenset({"attachment", "ai-title", "last-prompt", "queue-operation"})
_LEGACY_ONLY_TYPES = frozenset({"summary"})

# Any of these markers anywhere in content excludes the record (spec 4.2).
_EXCLUDING_MARKERS = (
    "<local-command-stdout>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)
_LEGACY_BASH_MARKERS = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")

KNOWN_WRAPPER_TAGS: frozenset[str] = frozenset(
    {
        "command-name",
        "command-message",
        "command-args",
        "local-command-stdout",
        "bash-input",
        "bash-stdout",
        "bash-stderr",
        "system-reminder",
    }
)

# Spec 8.4: origin/promptSource values outside the observed set fail closed.
_OBSERVED_ORIGIN_KINDS = frozenset({"human", "task-notification"})
_HUMAN_PROMPT_SOURCES = frozenset({"typed", "queued", "suggestion_accepted"})

_REMINDER_SPAN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_REMINDER_TAG = re.compile(r"</?system-reminder>")
_LEADING_TAG = re.compile(r"^<([A-Za-z][A-Za-z0-9_-]*)>")


class RecordOutcome(Enum):
    """Classification of one parsed JSONL record."""

    KEPT = "kept"
    EXCLUDED = "excluded"
    UNRECOGNIZED_USER = "unrecognized_user"
    UNKNOWN_TYPE = "unknown_type"


@dataclass(frozen=True)
class CleanedText:
    """Result of applying the spec 4.2 wrapper rules to one text payload."""

    kept: bool
    text: str = ""
    flags: tuple[str, ...] = ()
    unbalanced: bool = False
    wrapper_excluded: bool = False


def clean_wrapped_text(content: str) -> CleanedText:
    """Apply the wrapper handling rules to one string payload."""
    if content.lstrip().startswith("<command-name>"):
        return CleanedText(kept=False, wrapper_excluded=True)
    if any(marker in content for marker in _EXCLUDING_MARKERS):
        return CleanedText(kept=False, wrapper_excluded=True)
    stripped = _REMINDER_SPAN.sub("", content)
    if _REMINDER_TAG.search(stripped):
        return CleanedText(kept=False, unbalanced=True)
    text = stripped.strip()
    if not text:
        return CleanedText(kept=False)
    match = _LEADING_TAG.match(text)
    if match is None:
        return CleanedText(kept=True, text=text)
    if match.group(1) in KNOWN_WRAPPER_TAGS:
        return CleanedText(kept=False, wrapper_excluded=True)
    return CleanedText(kept=True, text=text, flags=("unknown_wrapper",))


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


@dataclass(frozen=True)
class ClassifiedRecord:
    """One record's classification plus the metadata discovery needs."""

    outcome: RecordOutcome
    text: str | None = None
    content_flags: tuple[str, ...] = ()
    record_uuid: str | None = None
    record_session_id: str | None = None
    timestamp: datetime | None = None
    app_version: str | None = None
    origin_human: bool = False
    unknown_origin: bool = False
    structural_warning: bool = False
    legacy_marker: bool = False
    current_marker: bool = False


def _string_field(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


def classify_record(record: dict[str, Any]) -> ClassifiedRecord:
    """Classify one parsed record against the spec 4.1/4.2 rules."""
    record_type = record.get("type")
    if not isinstance(record_type, str):
        return ClassifiedRecord(outcome=RecordOutcome.UNKNOWN_TYPE)

    timestamp = parse_timestamp(record.get("timestamp"))
    app_version = _string_field(record, "version")
    session_id = _string_field(record, "sessionId") or _string_field(record, "session_id")
    record_uuid = _string_field(record, "uuid")

    legacy = record_type in _LEGACY_ONLY_TYPES
    current = record_type in _CURRENT_ONLY_TYPES or "origin" in record
    if record.get("isSidechain") is True and record_type in {"user", "assistant"}:
        legacy = True

    def make(
        outcome: RecordOutcome,
        *,
        text: str | None = None,
        content_flags: tuple[str, ...] = (),
        origin_human: bool = False,
        unknown_origin: bool = False,
        warning: bool = False,
    ) -> ClassifiedRecord:
        return ClassifiedRecord(
            outcome=outcome,
            text=text,
            content_flags=content_flags,
            record_uuid=record_uuid,
            record_session_id=session_id,
            timestamp=timestamp,
            app_version=app_version,
            origin_human=origin_human,
            unknown_origin=unknown_origin,
            structural_warning=warning,
            legacy_marker=legacy,
            current_marker=current,
        )

    if record_type != "user":
        outcome = (
            RecordOutcome.EXCLUDED
            if record_type in KNOWN_RECORD_TYPES
            else RecordOutcome.UNKNOWN_TYPE
        )
        return make(outcome)

    message = record.get("message")
    if not isinstance(message, dict):
        return make(RecordOutcome.UNRECOGNIZED_USER)
    role = message.get("role")
    content = message.get("content")
    if not isinstance(role, str) or not isinstance(content, str | list) or record_uuid is None:
        return make(RecordOutcome.UNRECOGNIZED_USER)

    def excluded(*, unknown_origin: bool = False, warning: bool = False) -> ClassifiedRecord:
        return make(RecordOutcome.EXCLUDED, unknown_origin=unknown_origin, warning=warning)

    if role != "user":
        return excluded()
    if record.get("isMeta") is True:
        return excluded()
    if record.get("isSidechain") is True:
        return excluded()
    if record.get("isCompactSummary") is True:
        return excluded()
    user_type = record.get("userType")
    if user_type is not None and user_type != "external":
        return excluded()
    if "toolUseResult" in record:
        return excluded()

    origin_human = False
    origin = record.get("origin")
    if origin is not None:
        kind = origin.get("kind") if isinstance(origin, dict) else None
        if kind != "human":
            return excluded(unknown_origin=kind not in _OBSERVED_ORIGIN_KINDS)
        origin_human = True
    prompt_source = record.get("promptSource")
    if prompt_source is not None and prompt_source not in _HUMAN_PROMPT_SOURCES:
        return excluded(unknown_origin=prompt_source != "system")

    if isinstance(content, str):
        payloads = [content]
        tool_result = False
    else:
        payloads = []
        tool_result = False
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                tool_result = True
                break
            if block_type != "text":
                continue
            block_text = block.get("text")
            if isinstance(block_text, str):
                payloads.append(block_text)
    if tool_result:
        return excluded()

    parts: list[str] = []
    flags: set[str] = set()
    for payload in payloads:
        if any(marker in payload for marker in _LEGACY_BASH_MARKERS):
            legacy = True
        cleaned = clean_wrapped_text(payload)
        if cleaned.unbalanced:
            return excluded(warning=True)
        if cleaned.wrapper_excluded:
            return excluded()
        if cleaned.kept:
            parts.append(cleaned.text)
            flags.update(cleaned.flags)
    if not parts:
        return excluded()

    text = "\n\n".join(parts)
    if len(text) > PASTE_LENGTH_THRESHOLD:
        flags.add("possible_paste")
    return make(
        RecordOutcome.KEPT,
        text=text,
        content_flags=tuple(sorted(flags)),
        origin_human=origin_human,
    )


@dataclass(frozen=True)
class KeptCandidate:
    """One candidate user-authored utterance found in a transcript file."""

    record_uuid: str
    text: str
    text_sha256: str
    content_flags: tuple[str, ...]
    timestamp: datetime | None
    record_session_id: str | None
    origin_human: bool


@dataclass
class FileScan:
    """Aggregate result of scanning one session transcript file."""

    file_name: str
    session_id: str
    non_empty_lines: int = 0
    parsed_records: int = 0
    malformed_lines: int = 0
    truncated_tail: bool = False
    known_shape_records: int = 0
    unrecognized_user_records: int = 0
    unknown_origin_records: int = 0
    structural_warnings: int = 0
    legacy_marker: bool = False
    current_marker: bool = False
    app_version: str | None = None
    kept: list[KeptCandidate] = field(default_factory=list)

    @property
    def unsupported(self) -> bool:
        """Spec 7: over-threshold malformed lines or no known record shapes."""
        if self.non_empty_lines and self.malformed_lines * 10 > self.non_empty_lines:
            return True
        if self.parsed_records and self.known_shape_records == 0:
            return True
        return bool(self.parsed_records) and self.unrecognized_user_records * 2 > (
            self.parsed_records
        )

    @property
    def earliest_kept_timestamp(self) -> datetime | None:
        stamps = [candidate.timestamp for candidate in self.kept if candidate.timestamp]
        return min(stamps) if stamps else None


def normalized_text_sha256(text: str) -> str:
    """Hash of the NFC-normalized, stripped text; the fork-dedup equality key."""
    normalized = unicodedata.normalize("NFC", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def scan_transcript(path: Path) -> FileScan:
    """Stream-classify one session JSONL file.

    A final line that fails to parse is treated as an interrupted append and
    dropped silently (spec 6.2); mid-file failures count toward the 10%
    malformed threshold.
    """
    scan = FileScan(file_name=path.name, session_id=path.stem)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        lines = handle.read().split("\n")
    non_empty = [(index, line) for index, line in enumerate(lines) if line.strip()]
    scan.non_empty_lines = len(non_empty)
    last_index = non_empty[-1][0] if non_empty else -1
    seen_uuids: dict[str, str] = {}
    for index, line in non_empty:
        try:
            parsed = json.loads(line)
        except ValueError:
            if index == last_index:
                scan.truncated_tail = True
            else:
                scan.malformed_lines += 1
            continue
        if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
            scan.malformed_lines += 1
            continue
        scan.parsed_records += 1
        classified = classify_record(parsed)
        if classified.app_version is not None:
            scan.app_version = classified.app_version
        scan.legacy_marker = scan.legacy_marker or classified.legacy_marker
        scan.current_marker = scan.current_marker or classified.current_marker
        if classified.structural_warning:
            scan.structural_warnings += 1
        if classified.unknown_origin:
            scan.unknown_origin_records += 1
        if classified.outcome is RecordOutcome.UNKNOWN_TYPE:
            continue
        if classified.outcome is RecordOutcome.UNRECOGNIZED_USER:
            scan.unrecognized_user_records += 1
            continue
        scan.known_shape_records += 1
        if classified.outcome is not RecordOutcome.KEPT:
            continue
        if classified.text is None or classified.record_uuid is None:
            continue
        text_hash = normalized_text_sha256(classified.text)
        prior = seen_uuids.get(classified.record_uuid)
        if prior is not None:
            if prior != text_hash:
                scan.structural_warnings += 1
            continue
        seen_uuids[classified.record_uuid] = text_hash
        flags = classified.content_flags
        if (
            classified.record_session_id is not None
            and classified.record_session_id != scan.session_id
        ):
            flags = (*flags, "session_id_mismatch")
        scan.kept.append(
            KeptCandidate(
                record_uuid=classified.record_uuid,
                text=classified.text,
                text_sha256=text_hash,
                content_flags=flags,
                timestamp=classified.timestamp,
                record_session_id=classified.record_session_id,
                origin_human=classified.origin_human,
            )
        )
    if scan.truncated_tail and scan.kept:
        last = scan.kept[-1]
        scan.kept[-1] = replace(last, content_flags=(*last.content_flags, "truncated_tail_dropped"))
    return scan
