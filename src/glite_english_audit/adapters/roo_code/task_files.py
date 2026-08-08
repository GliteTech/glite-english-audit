"""Roo Code per-task file classification.

Implements the record schema, wrapper, inclusion, and failure rules from
``specifications/sources/roo_code.md`` (sections 3, 4, and 7). Everything is
feature-detected per record; nothing branches on an extension version. Only
:func:`scan_task` touches the filesystem, and it opens exactly the three
allowlisted per-task file names through the caller's recording callback.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

API_HISTORY_NAME = "api_conversation_history.json"
HISTORY_ITEM_NAME = "history_item.json"
UI_MESSAGES_NAME = "ui_messages.json"
# Vestigial pre-fork file name (spec 8.5): counted when seen, never parsed.
LEGACY_UI_NAME = "claude_messages.json"

# Spec 4.2: the only recognized user-text carriers across all generations.
_WRAPPER_SPAN = re.compile(r"<(task|feedback|answer|user_message)>(.*?)</\1>", re.DOTALL)
_WRAPPER_OPEN = re.compile(r"<(?:task|feedback|answer|user_message)>")
_G1_MARKER = re.compile(r"<(?:task|feedback|answer)>")
_USER_MESSAGE_TAG = "<user_message>"
_NESTED_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9_.-]*(?:\s[^<>]*)?/?>")
_ENVIRONMENT_DETAILS = "<environment_details>"

# Spec 4.3: upstream mention expansion rewrites the user's own sentence, so a
# span carrying one of these markers is "cleaned", never "verbatim".
_MENTION_MARKERS = ("(see below for file content)", "<file_content")
_MENTION_QUOTED = re.compile(r"'[^'\n]*'\s*\(see below")


class TaskStatus(Enum):
    """Classification of one task directory (spec 4.1 and 7)."""

    OK = "ok"
    EMPTY = "empty"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    OVERSIZED = "oversized"
    EXCLUDED_SUBTASK = "excluded_subtask"


@dataclass(frozen=True)
class SpanCandidate:
    """One candidate user-text span found inside a task's API history."""

    record_index: int
    block_index: int
    span_index: int
    text: str
    carrier: str
    cleaned: bool
    flags: tuple[str, ...]
    timestamp: datetime | None
    is_initial: bool


@dataclass(frozen=True)
class WrapperSpan:
    """One balanced wrapper span extracted from a text payload."""

    text: str
    unknown_wrapper: bool
    cleaned: bool


@dataclass
class TaskScan:
    """Aggregate result of scanning one task directory."""

    dir_name: str
    status: TaskStatus
    parsed_records: int = 0
    unrecognized_records: int = 0
    unbalanced_wrappers: int = 0
    candidates: list[SpanCandidate] = field(default_factory=list)
    new_task_messages: list[str] = field(default_factory=list)
    history_file_present: bool = False
    has_history_item: bool = False
    metadata_unreadable: bool = False
    subtask_by_metadata: bool = False
    history_ts: datetime | None = None
    generation: str | None = None
    has_record_ts: bool = False
    legacy_unmigrated: bool = False
    ui_min_ts: datetime | None = None
    ui_feedback_hashes: tuple[str, ...] = ()


def normalized_text_sha256(text: str) -> str:
    """Hash of the NFC-normalized, stripped text; the cross-check equality key."""
    normalized = unicodedata.normalize("NFC", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_epoch_ms(value: object) -> datetime | None:
    """Parse an epoch-milliseconds timestamp; anything implausible becomes None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def is_mention_cleaned(text: str) -> bool:
    """Whether a span carries a mention-expansion rewrite marker (spec 4.3)."""
    if any(marker in text for marker in _MENTION_MARKERS):
        return True
    return bool(_MENTION_QUOTED.search(text))


def scan_wrappers(payload: str) -> tuple[list[WrapperSpan], bool]:
    """All balanced wrapper spans of one payload, plus the unbalanced flag.

    An opening wrapper tag left over after removing every balanced span means
    the block is excluded whole (spec 4.2 wrapper integrity).
    """
    matches = list(_WRAPPER_SPAN.finditer(payload))
    remainder = _WRAPPER_SPAN.sub("", payload)
    if _WRAPPER_OPEN.search(remainder):
        return [], True
    spans: list[WrapperSpan] = []
    for match in matches:
        inner = match.group(2).strip()
        # Injected context must never ride into extraction, even nested.
        if not inner or _ENVIRONMENT_DETAILS in inner:
            continue
        spans.append(
            WrapperSpan(
                text=inner,
                unknown_wrapper=bool(_NESTED_TAG.search(inner)),
                cleaned=is_mention_cleaned(inner),
            )
        )
    return spans, False


def parse_status_feedback(payload: str) -> tuple[bool, str | None]:
    """Spec 4.2 G2/G3 rule: JSON object with a ``status`` key; ``feedback`` text."""
    trimmed = payload.strip()
    if not trimmed.startswith("{") or not trimmed.endswith("}"):
        return False, None
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return False, None
    if not isinstance(parsed, dict) or "status" not in parsed:
        return False, None
    feedback = parsed.get("feedback")
    if isinstance(feedback, str) and feedback.strip():
        return True, feedback.strip()
    return True, None


def _text_payloads(content: str | list[Any]) -> list[tuple[int, str, bool]]:
    """(block index, text, is tool_result) for every recognized text payload."""
    if isinstance(content, str):
        return [(0, content, False)]
    payloads: list[tuple[int, str, bool]] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                payloads.append((block_index, text, False))
        elif block_type == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                payloads.append((block_index, inner, True))
            elif isinstance(inner, list):
                for part in inner:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            payloads.append((block_index, part_text, True))
    return payloads


def _collect_new_task_messages(content: str | list[Any], scan: TaskScan) -> None:
    """Spec 4.1 mitigation input: ``new_task`` tool_use ``message`` arguments."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "new_task":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        message = tool_input.get("message")
        if isinstance(message, str) and message.strip():
            scan.new_task_messages.append(message.strip())


def _classify_elements(elements: list[Any], scan: TaskScan) -> None:
    """Apply the spec 4.2 per-record rules to one parsed API history array."""
    total = len(elements)
    g1 = g2 = g3 = False
    has_plain_user_text = False
    first_user_index: int | None = None
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            scan.unrecognized_records += 1
            continue
        role = element.get("role")
        content = element.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str | list):
            scan.unrecognized_records += 1
            continue
        scan.parsed_records += 1
        timestamp = parse_epoch_ms(element.get("ts"))
        if timestamp is not None:
            scan.has_record_ts = True
        if role == "assistant":
            _collect_new_task_messages(content, scan)
            continue
        if first_user_index is None:
            first_user_index = index
        if element.get("isSummary") is True or element.get("isTruncationMarker") is True:
            continue
        if "type" in element:
            # Spec 4.2 rule 3: reasoning-persistence records are excluded.
            continue
        is_initial = index == first_user_index
        span_counters: dict[int, int] = {}
        for block_index, text, is_tool_result in _text_payloads(content):
            if _G1_MARKER.search(text):
                g1 = True
            if _USER_MESSAGE_TAG in text:
                g3 = True
            spans, unbalanced = scan_wrappers(text)
            if unbalanced:
                scan.unbalanced_wrappers += 1
                continue
            for span in spans:
                span_index = span_counters.get(block_index, 0)
                span_counters[block_index] = span_index + 1
                scan.candidates.append(
                    SpanCandidate(
                        record_index=index,
                        block_index=block_index,
                        span_index=span_index,
                        text=span.text,
                        carrier="wrapper",
                        cleaned=span.cleaned,
                        flags=("unknown_wrapper",) if span.unknown_wrapper else (),
                        timestamp=timestamp,
                        is_initial=is_initial,
                    )
                )
            if is_tool_result:
                is_status, feedback = parse_status_feedback(text)
                if is_status:
                    g2 = True
                if feedback is not None:
                    span_index = span_counters.get(block_index, 0)
                    span_counters[block_index] = span_index + 1
                    scan.candidates.append(
                        SpanCandidate(
                            record_index=index,
                            block_index=block_index,
                            span_index=span_index,
                            text=feedback,
                            carrier="json_feedback",
                            cleaned=is_mention_cleaned(feedback),
                            flags=(),
                            timestamp=timestamp,
                            is_initial=is_initial,
                        )
                    )
            elif text.strip() and not spans:
                has_plain_user_text = True
    scan.generation = (
        "g3-user-message" if g3 else "g2-native-json" if g2 else "g1-xml" if g1 else None
    )
    if scan.unrecognized_records * 10 > total:
        # Spec 7: over ten percent unrecognized elements fails the task closed.
        scan.status = TaskStatus.UNSUPPORTED
        scan.candidates = []
        return
    if has_plain_user_text and not scan.candidates and scan.generation is None:
        # Spec 7: user text with no recognized carrier convention, anywhere.
        scan.status = TaskStatus.UNSUPPORTED
        return
    scan.status = TaskStatus.OK


def _read_history_item(task_dir: Path, scan: TaskScan, record_open: Callable[[Path], None]) -> None:
    """Read only ts/id/parentTaskId/rootTaskId; other fields are parsed past."""
    path = task_dir / HISTORY_ITEM_NAME
    if not path.is_file() or path.is_symlink():
        return
    scan.history_file_present = True
    record_open(path)
    try:
        payload = json.loads(path.read_bytes())
    except ValueError:
        scan.metadata_unreadable = True
        return
    if not isinstance(payload, dict):
        scan.metadata_unreadable = True
        return
    scan.has_history_item = True
    item_id = payload.get("id")
    own_id = item_id if isinstance(item_id, str) and item_id else scan.dir_name
    parent = payload.get("parentTaskId")
    root_id = payload.get("rootTaskId")
    if (isinstance(parent, str) and parent) or (
        isinstance(root_id, str) and root_id and root_id != own_id
    ):
        scan.subtask_by_metadata = True
    scan.history_ts = parse_epoch_ms(payload.get("ts"))


def _read_ui_messages(task_dir: Path, scan: TaskScan, record_open: Callable[[Path], None]) -> None:
    """Structure-only read: ts values and hashes of user_feedback text."""
    path = task_dir / UI_MESSAGES_NAME
    if not path.is_file() or path.is_symlink():
        return
    record_open(path)
    try:
        payload = json.loads(path.read_bytes())
    except ValueError:
        return
    if not isinstance(payload, list):
        return
    stamps: list[datetime] = []
    hashes: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        stamp = parse_epoch_ms(entry.get("ts"))
        if stamp is not None:
            stamps.append(stamp)
        if entry.get("type") == "say" and entry.get("say") == "user_feedback":
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                hashes.append(normalized_text_sha256(text))
    scan.ui_min_ts = min(stamps) if stamps else None
    scan.ui_feedback_hashes = tuple(hashes)


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def scan_task(
    task_dir: Path,
    *,
    max_file_bytes: int,
    record_open: Callable[[Path], None],
) -> TaskScan:
    """Scan one task directory, opening only the three allowlisted files."""
    scan = TaskScan(dir_name=task_dir.name, status=TaskStatus.EMPTY)
    scan.legacy_unmigrated = (task_dir / LEGACY_UI_NAME).is_file()
    _read_history_item(task_dir, scan, record_open)
    api_path = task_dir / API_HISTORY_NAME
    if not api_path.is_file() or api_path.is_symlink():
        if scan.subtask_by_metadata:
            scan.status = TaskStatus.EXCLUDED_SUBTASK
        return scan
    if api_path.stat().st_size > max_file_bytes:
        # Spec 6.1 large-file guard: counted and skipped, never loaded whole.
        scan.status = TaskStatus.OVERSIZED
        return scan
    record_open(api_path)
    try:
        payload = json.loads(api_path.read_bytes())
    except ValueError:
        scan.status = TaskStatus.MALFORMED
        return scan
    if not isinstance(payload, list):
        scan.status = TaskStatus.MALFORMED
        return scan
    if payload:
        _classify_elements(payload, scan)
        _read_ui_messages(task_dir, scan, record_open)
        fallback = scan.history_ts or scan.ui_min_ts or _file_mtime(api_path)
        if fallback is not None:
            scan.candidates = [
                replace(candidate, timestamp=fallback) if candidate.timestamp is None else candidate
                for candidate in scan.candidates
            ]
    if scan.subtask_by_metadata:
        scan.status = TaskStatus.EXCLUDED_SUBTASK
        scan.candidates = []
    return scan


def apply_subtask_argument_match(scans: list[TaskScan]) -> None:
    """Spec 4.1 pre-3.50 mitigation, deterministic within one instance.

    A task without ``history_item.json`` whose initial wrapper text exactly
    matches any collected ``new_task`` message argument is a delegated subtask
    and is excluded whole.
    """
    messages = {message for scan in scans for message in scan.new_task_messages}
    if not messages:
        return
    for scan in scans:
        if scan.status is not TaskStatus.OK or scan.has_history_item:
            continue
        initial = next(
            (c for c in scan.candidates if c.is_initial and c.carrier == "wrapper"),
            None,
        )
        if initial is not None and initial.text.strip() in messages:
            scan.status = TaskStatus.EXCLUDED_SUBTASK
            scan.candidates = []
