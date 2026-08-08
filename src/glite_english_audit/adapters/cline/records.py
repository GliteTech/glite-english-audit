"""Cline conversation-store record classification.

Implements the record schema, inclusion, exclusion, wrapper, and failure
rules from ``specifications/sources/cline.md`` (sections 3, 4, 5, and 7) for
the three on-disk generations: G1 (``claude_messages.json`` UI name), G2
(``tasks/<taskId>/api_conversation_history.json``), and G3 (SDK
``sessions/<sessionId>/<sessionId>.messages.json``). Files are fingerprinted
by shape; nothing branches on an application version. All filesystem reads go
through the caller-provided ``read_bytes`` callable so the adapter's
allowlist gate and opened-path audit wrap every open.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from glite_english_audit.artifacts.enums import TextStatus

API_HISTORY_NAME = "api_conversation_history.json"
UI_MESSAGES_NAME = "ui_messages.json"
LEGACY_UI_MESSAGES_NAME = "claude_messages.json"
MESSAGES_SUFFIX = ".messages.json"

# Spec 6.1: conversation files above this ceiling are counted oversized and
# never loaded whole (unbounded histories are a known upstream failure mode).
MAX_CONVERSATION_FILE_BYTES = 256 * 1024 * 1024

# Spec 3.1: the only user-text carriers inside G1/G2 user-role records.
G2_WRAPPER_KINDS = ("task", "feedback", "answer", "user_message")

# Spec 3.1/4.2: whole blocks starting with an injected tag are never user text.
_INJECTED_BLOCK_PREFIXES = (
    "<environment_details>",
    "<file_content",
    "<folder_content",
    "<url_content",
    "<workspace_diagnostics>",
)
# Spec 4.4: the upstream mention-rewrite marker phrase; affected spans are
# `cleaned`. Spans that still carry a mention tag are dropped fail-closed so
# no injected content can survive into an utterance (spec 6.4).
_MENTION_MARKER = re.compile(r"\(see below for [^)]{0,60}content\)")
_MENTION_TAGS = ("<file_content", "<url_content", "<folder_content")
_EXPLICIT_INSTRUCTIONS = re.compile(
    r"<explicit_instructions\b[^>]*>.*?</explicit_instructions>", re.DOTALL
)
_LEADING_TAG = re.compile(r"^<([A-Za-z][A-Za-z0-9_-]*)[\s>]")
_KNOWN_INNER_TAGS = frozenset({"explicit_instructions"})

# Spec 3.4/4.3: G3 wrappers.
_MODE_NOTICE = re.compile(r"<mode_notice>.*?</mode_notice>", re.DOTALL)
_USER_INPUT = re.compile(r"<user_input\b[^>]*>(.*?)</user_input>", re.DOTALL)
_USER_COMMAND_TAG = "<user_command"
# Spec 4.3.5: known runtime-composed prefixes (extensible denylist).
RUNTIME_COMPOSED_PREFIXES = ("System-delivered teammate async run updates:",)

# taskId / ts plausibility window: epoch milliseconds between 2015 and 2100.
_EPOCH_MS_MIN = 1_420_070_400_000
_EPOCH_MS_MAX = 4_102_444_800_000

ReadBytes = Callable[[Path], bytes]


class OversizedFileError(Exception):
    """A conversation file exceeds the large-file guard ceiling."""


class UnitStatus(Enum):
    """Outcome of scanning one task or session directory."""

    SUPPORTED = "supported"
    EXCLUDED = "excluded"
    UI_ONLY = "ui_only_task"
    MALFORMED = "malformed_file"
    UNSUPPORTED = "unsupported_schema"
    OVERSIZED = "oversized_file"


@dataclass(frozen=True)
class UnitCandidate:
    """One candidate user-authored span found in a unit."""

    record_ref: str
    wrapper_kind: str | None
    text: str
    text_hash: str
    text_status: TextStatus
    content_flags: tuple[str, ...]
    timestamp: datetime | None
    authorship_basis: str
    authorship_confidence: float


@dataclass
class UnitScan:
    """Aggregate result of scanning one task or session directory."""

    unit_id: str
    unit_relpath: str
    generation: str
    status: UnitStatus
    source_file_relpath: str | None = None
    record_count: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    new_task_arguments: tuple[str, ...] = ()
    candidates: list[UnitCandidate] = field(default_factory=list)

    def bump(self, name: str, amount: int = 1) -> None:
        """Increment one named exclusion or failure counter."""
        self.counters[name] = self.counters.get(name, 0) + amount

    def fail(self, status: UnitStatus) -> "UnitScan":
        """Mark the unit failed closed: no candidates survive."""
        self.status = status
        self.bump(status.value)
        self.candidates = []
        return self


def normalized_text_sha256(text: str) -> str:
    """Hash of the NFC-normalized, stripped text; the dedup equality key."""
    normalized = unicodedata.normalize("NFC", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    """Whitespace-insensitive comparison form (spec 6.4 cross-check)."""
    return " ".join(text.split())


def parse_epoch_ms(value: object) -> datetime | None:
    """Parse an epoch-milliseconds value; implausible values become None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    ms = int(value)
    if not _EPOCH_MS_MIN <= ms <= _EPOCH_MS_MAX:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def parse_task_id_timestamp(task_id: str) -> datetime | None:
    """Task-start fallback: the taskId is ``Date.now().toString()`` (spec 5)."""
    if not task_id.isdigit():
        return None
    return parse_epoch_ms(int(task_id))


def parse_iso_timestamp(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp; unparsable values become None."""
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
class SpanCandidate:
    """One wrapper span kept from a G1/G2 payload."""

    kind: str
    text: str
    flags: tuple[str, ...]
    cleaned: bool


def _wrapper_spans(payload: str, kind: str) -> list[str] | None:
    """Inner spans for one wrapper kind; None marks an unbalanced wrapper."""
    open_tag = f"<{kind}>"
    close_tag = f"</{kind}>"
    if payload.count(open_tag) != payload.count(close_tag):
        return None
    pattern = re.compile(re.escape(open_tag) + r"(.*?)" + re.escape(close_tag), re.DOTALL)
    return [match.group(1) for match in pattern.finditer(payload)]


def extract_g2_spans(payload: str) -> tuple[list[SpanCandidate], bool, bool]:
    """Kept spans of one payload: (spans, unbalanced, mention_tag_dropped)."""
    if any(payload.lstrip().startswith(prefix) for prefix in _INJECTED_BLOCK_PREFIXES):
        return [], False, False
    spans: list[SpanCandidate] = []
    mention_dropped = False
    for kind in G2_WRAPPER_KINDS:
        inner_spans = _wrapper_spans(payload, kind)
        if inner_spans is None:
            return [], True, False
        for inner in inner_spans:
            text = _EXPLICIT_INSTRUCTIONS.sub("", inner).strip()
            if not text:
                continue
            if any(tag in text for tag in _MENTION_TAGS):
                mention_dropped = True
                continue
            flags: tuple[str, ...] = ()
            match = _LEADING_TAG.match(text)
            if match is not None and match.group(1) not in _KNOWN_INNER_TAGS:
                flags = ("unknown_wrapper",)
            cleaned = _MENTION_MARKER.search(text) is not None
            spans.append(SpanCandidate(kind=kind, text=text, flags=flags, cleaned=cleaned))
    return spans, False, mention_dropped


def _payload_strings(content: object) -> list[str] | None:
    """String payloads of one user-role record, or None for unknown shapes."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return None
    payloads: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                payloads.append(text)
        elif block_type == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                payloads.append(inner)
            elif isinstance(inner, list):
                for part in inner:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            payloads.append(part_text)
    return payloads


def _new_task_arguments(record: dict[str, Any]) -> list[str]:
    """``new_task`` tool_use arguments in one record (spec 4.2 mitigation)."""
    content = record.get("content")
    arguments: list[str] = []
    if not isinstance(content, list):
        return arguments
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "new_task":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        for key in ("context", "message"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                arguments.append(value)
    return arguments


@dataclass(frozen=True)
class UiJoin:
    """Structure-only join data from a UI stream (spec 5, 6.4)."""

    task_timestamp: datetime | None
    task_text: str | None
    feedback_timestamps: tuple[datetime | None, ...]


def _read_ui_join(path: Path, read_bytes: ReadBytes) -> UiJoin | None:
    """Timestamps plus the initial-task text from a UI stream, or None."""
    try:
        raw = read_bytes(path)
    except (OversizedFileError, OSError):
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, list):
        return None
    task_seen = False
    task_timestamp: datetime | None = None
    task_text: str | None = None
    feedback: list[datetime | None] = []
    for entry in document:
        if not isinstance(entry, dict) or entry.get("type") != "say":
            continue
        say = entry.get("say")
        if say == "task" and not task_seen:
            task_seen = True
            task_timestamp = parse_epoch_ms(entry.get("ts"))
            text = entry.get("text")
            task_text = text if isinstance(text, str) else None
        elif say in ("user_feedback", "user_feedback_diff"):
            feedback.append(parse_epoch_ms(entry.get("ts")))
    return UiJoin(
        task_timestamp=task_timestamp,
        task_text=task_text,
        feedback_timestamps=tuple(feedback),
    )


def _read_json_document(path: Path, read_bytes: ReadBytes) -> tuple[Any, UnitStatus | None]:
    """Parse one allowlisted JSON file; the second item is a failure status."""
    try:
        raw = read_bytes(path)
    except OversizedFileError:
        return None, UnitStatus.OVERSIZED
    except OSError:
        return None, UnitStatus.MALFORMED
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError):
        return None, UnitStatus.MALFORMED


def scan_task_dir(task_dir: Path, unit_relpath: str, read_bytes: ReadBytes) -> UnitScan:
    """Scan one G1/G2 ``tasks/<taskId>/`` directory (spec 4.1, 4.2, 5)."""
    unit_id = task_dir.name
    ui_name: str | None = None
    if (task_dir / UI_MESSAGES_NAME).is_file():
        ui_name = UI_MESSAGES_NAME
    elif (task_dir / LEGACY_UI_MESSAGES_NAME).is_file():
        ui_name = LEGACY_UI_MESSAGES_NAME
    generation = "g1-claude-messages" if ui_name == LEGACY_UI_MESSAGES_NAME else "g2-task-store"
    scan = UnitScan(
        unit_id=unit_id,
        unit_relpath=unit_relpath,
        generation=generation,
        status=UnitStatus.SUPPORTED,
    )

    api_path = task_dir / API_HISTORY_NAME
    if not api_path.is_file():
        if ui_name is not None:
            document, failure = _read_json_document(task_dir / ui_name, read_bytes)
            if failure is None and isinstance(document, list):
                return scan.fail(UnitStatus.UI_ONLY)
        return scan.fail(UnitStatus.MALFORMED)

    document, failure = _read_json_document(api_path, read_bytes)
    if failure is not None:
        return scan.fail(failure)
    if not isinstance(document, list):
        return scan.fail(UnitStatus.MALFORMED)

    scan.source_file_relpath = f"{unit_relpath}/{API_HISTORY_NAME}"
    new_task_args: list[str] = []
    spans: list[tuple[int, int, SpanCandidate]] = []
    unrecognized = 0
    for record_index, record in enumerate(document):
        scan.record_count += 1
        if not isinstance(record, dict):
            unrecognized += 1
            continue
        new_task_args.extend(_new_task_arguments(record))
        if record.get("role") != "user":
            continue
        payloads = _payload_strings(record.get("content"))
        if payloads is None:
            unrecognized += 1
            scan.bump("unrecognized_user_record")
            continue
        span_index = 0
        for payload in payloads:
            kept, unbalanced, mention_dropped = extract_g2_spans(payload)
            if unbalanced:
                scan.bump("unbalanced_wrapper")
                continue
            if mention_dropped:
                scan.bump("mention_expansion_span")
            for span in kept:
                spans.append((record_index, span_index, span))
                span_index += 1
    scan.new_task_arguments = tuple(new_task_args)
    if scan.record_count and unrecognized * 10 > scan.record_count:
        return scan.fail(UnitStatus.UNSUPPORTED)

    ui_join = _read_ui_join(task_dir / ui_name, read_bytes) if ui_name is not None else None
    task_fallback = parse_task_id_timestamp(unit_id)
    initial_seen = False
    feedback_index = 0
    for record_index, span_index, span in spans:
        flags = span.flags
        if span.kind == "task" and not initial_seen:
            initial_seen = True
            timestamp = (
                ui_join.task_timestamp
                if ui_join is not None and ui_join.task_timestamp is not None
                else task_fallback
            )
            if (
                ui_join is not None
                and ui_join.task_text is not None
                and normalize_whitespace(ui_join.task_text) != normalize_whitespace(span.text)
            ):
                # Spec 6.4: non-fatal cross-check mismatch; the API copy wins.
                flags = (*flags, "channel_mismatch")
        else:
            timestamp = None
            if ui_join is not None and feedback_index < len(ui_join.feedback_timestamps):
                timestamp = ui_join.feedback_timestamps[feedback_index]
            feedback_index += 1
            if timestamp is None:
                timestamp = task_fallback
        scan.candidates.append(
            UnitCandidate(
                record_ref=f"r{record_index:04d}-s{span_index:02d}",
                wrapper_kind=span.kind,
                text=span.text,
                text_hash=normalized_text_sha256(span.text),
                text_status=TextStatus.CLEANED if span.cleaned else TextStatus.VERBATIM,
                content_flags=flags,
                timestamp=timestamp,
                authorship_basis="explicit_user_role+wrapper",
                authorship_confidence=0.9,
            )
        )
    return scan


def _g3_message_candidate(
    message: dict[str, Any],
    scan: UnitScan,
    fallback_timestamp: datetime | None,
) -> UnitCandidate | None:
    """Apply the spec 4.3 rules to one G3 user-role message."""
    content = message.get("content")
    if not isinstance(content, list):
        scan.bump("unrecognized_user_record")
        return None
    texts: list[str] = []
    has_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            has_tool_result = True
        elif block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    if has_tool_result:
        # Spec 4.3.2: tool-result carriers are generated traffic; a message
        # mixing tool results with prose is excluded fail-closed.
        if any(text.strip() for text in texts):
            scan.bump("mixed_user_message")
        return None

    parts: list[str] = []
    flags: set[str] = set()
    unwrapped = False
    for text in texts:
        if any(text.lstrip().startswith(prefix) for prefix in _INJECTED_BLOCK_PREFIXES):
            continue
        if _USER_COMMAND_TAG in text:
            scan.bump("user_command_message")
            continue
        stripped = _MODE_NOTICE.sub("", text)
        match = _USER_INPUT.search(stripped)
        if match is not None:
            candidate = match.group(1).strip()
        else:
            candidate = stripped.strip()
            if candidate:
                unwrapped = True
                flags.add("unwrapped_user_text")
        if not candidate:
            continue
        if any(candidate.startswith(prefix) for prefix in RUNTIME_COMPOSED_PREFIXES):
            scan.bump("runtime_composed_prefix")
            continue
        parts.append(candidate)
    if not parts:
        return None

    message_id = message.get("id")
    record_ref = message_id if isinstance(message_id, str) and message_id else None
    if record_ref is None:
        record_ref = f"m{scan.record_count:04d}"
    text_value = "\n\n".join(parts)
    timestamp = parse_epoch_ms(message.get("ts"))
    return UnitCandidate(
        record_ref=record_ref,
        wrapper_kind=None,
        text=text_value,
        text_hash=normalized_text_sha256(text_value),
        text_status=TextStatus.UNKNOWN if unwrapped else TextStatus.VERBATIM,
        content_flags=tuple(sorted(flags)),
        timestamp=timestamp if timestamp is not None else fallback_timestamp,
        authorship_basis="explicit_user_role+user_input",
        authorship_confidence=0.7 if unwrapped else 0.9,
    )


def scan_session_dir(session_dir: Path, unit_relpath: str, read_bytes: ReadBytes) -> UnitScan:
    """Scan one G3 ``sessions/<sessionId>/`` directory (spec 4.1, 4.3, 5)."""
    unit_id = session_dir.name
    scan = UnitScan(
        unit_id=unit_id,
        unit_relpath=unit_relpath,
        generation="g3-sdk-sessions",
        status=UnitStatus.SUPPORTED,
    )
    lead_name = f"{unit_id}{MESSAGES_SUFFIX}"
    messages_files: list[str] = []
    try:
        for entry in sorted(session_dir.iterdir()):
            if entry.is_file() and not entry.is_symlink() and entry.name.endswith(MESSAGES_SUFFIX):
                messages_files.append(entry.name)
    except OSError:
        return scan.fail(UnitStatus.MALFORMED)
    # Spec 4.1: only the name-matching lead file is ever an extraction source;
    # other agents' artifacts are counted by name and never opened here.
    non_lead = sum(1 for name in messages_files if name != lead_name)
    if non_lead:
        scan.bump("non_lead_messages_file", non_lead)

    manifest_path = session_dir / f"{unit_id}.json"
    started_at: datetime | None = None
    if manifest_path.is_file():
        manifest, failure = _read_json_document(manifest_path, read_bytes)
        if failure is None and isinstance(manifest, dict):
            # Only eligibility and timestamp fields are used; private fields
            # such as prompt/cwd are parsed past, never returned (spec 3.5).
            if manifest.get("interactive") is False:
                scan.status = UnitStatus.EXCLUDED
                scan.bump("non_interactive_session")
                return scan
            started_at = parse_iso_timestamp(manifest.get("started_at"))

    lead_path = session_dir / lead_name
    if not lead_path.is_file():
        if non_lead:
            scan.status = UnitStatus.EXCLUDED
            return scan
        return scan.fail(UnitStatus.MALFORMED)
    payload, failure = _read_json_document(lead_path, read_bytes)
    if failure is not None:
        return scan.fail(failure)
    if not isinstance(payload, dict):
        return scan.fail(UnitStatus.MALFORMED)
    if payload.get("version") != 1:
        # Spec 7: the vendor promises a version bump on breaking change.
        return scan.fail(UnitStatus.UNSUPPORTED)
    agent = payload.get("agent")
    if agent is not None and agent != "lead":
        scan.status = UnitStatus.EXCLUDED
        scan.bump("non_lead_session")
        return scan
    payload_session = payload.get("sessionId")
    if isinstance(payload_session, str) and payload_session != unit_id:
        scan.status = UnitStatus.EXCLUDED
        scan.bump("session_id_mismatch")
        return scan
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return scan.fail(UnitStatus.MALFORMED)

    scan.source_file_relpath = f"{unit_relpath}/{lead_name}"
    unrecognized = 0
    fallback = started_at
    if fallback is None:
        try:
            fallback = datetime.fromtimestamp(lead_path.stat().st_mtime, tz=UTC)
        except OSError:
            fallback = None
    for message in messages:
        scan.record_count += 1
        if not isinstance(message, dict):
            unrecognized += 1
            continue
        if message.get("role") != "user":
            continue
        candidate = _g3_message_candidate(message, scan, fallback)
        if candidate is not None:
            scan.candidates.append(candidate)
    if scan.record_count and unrecognized * 10 > scan.record_count:
        return scan.fail(UnitStatus.UNSUPPORTED)
    return scan
