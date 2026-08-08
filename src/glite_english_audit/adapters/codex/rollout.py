"""Parsing for Codex CLI rollout JSONL session files.

Implements the record schema, the inclusion and exclusion rules, and the
per-file channel feature detection from ``specifications/sources/codex.md``
(sections 3, 5, and 6). Parsing is local and read-only; scan results carry
candidate text only to the adapter, never to logs or agent-facing output.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

ROLLOUT_FILE_NAME = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.jsonl"
)

_FILENAME_UUID = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)

# Injected wrapper markers (spec 5.2). The list is exact: only these forms are
# excluded, so no heuristic can silently drop real user text.
_INJECTION_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<ENVIRONMENT_CONTEXT>",
    "<USER_INSTRUCTIONS>",
    "<turn_context>",
    "<permissions",
    "# AGENTS.md",
)

_SUPPORTED_HISTORY_MODES = frozenset({"legacy", "paginated"})
_SUBAGENT_MARKER_KEYS = ("agent_nickname", "agent_path", "agent_role")

_CONFIDENCE_KIND_PLAIN = 0.95
_CONFIDENCE_NO_KIND = 0.9
_CONFIDENCE_CHANNEL_B = 0.85
_BASIS_KIND_PLAIN = "codex event_msg user_message with kind=plain"
_BASIS_NO_KIND = "codex event_msg user_message without kind; injection tag filter applied"
_BASIS_CHANNEL_B = "codex response_item user message input_text; injection tag filter applied"


class FileStatus(Enum):
    """Classification of one rollout file after a scan."""

    SUPPORTED = "supported"
    EMPTY = "empty"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Candidate:
    """One included user-authored candidate line."""

    line_index: int
    text: str
    timestamp: datetime | None
    confidence: float
    basis: str


@dataclass(frozen=True)
class FileScan:
    """Everything discovery and extraction need to know about one file."""

    relative_path: str
    status: FileStatus
    generation: str
    session_id: str | None = None
    eligible: bool = True
    truncated_tail: bool = False
    channel: str | None = None
    channel_mismatch: bool = False
    forked: bool = False
    session_id_mismatch: bool = False
    cli_versions: tuple[str, ...] = ()
    pre_filter_count: int = 0
    candidates: tuple[Candidate, ...] = ()


def filename_session_uuid(name: str) -> str | None:
    """The session UUID embedded in a rollout filename, if present."""
    match = _FILENAME_UUID.search(name)
    return match.group(1) if match else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_injected(text: str) -> bool:
    """The tag filter from spec 5.2, applied to trimmed candidate text."""
    trimmed = text.strip()
    if trimmed.startswith(_INJECTION_PREFIXES):
        return True
    if "<environment_context>" in trimmed[:200]:
        return True
    return trimmed.splitlines()[0].strip() == "# AGENTS"


def _contains_subagent(value: object) -> bool:
    """True when a ``subagent`` key appears at any nesting depth."""
    if isinstance(value, dict):
        return any(key == "subagent" or _contains_subagent(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_subagent(item) for item in value)
    return False


def _joined_input_text(payload: dict[str, object]) -> str:
    """Concatenated ``input_text`` parts of one message payload."""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "input_text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _unsupported_generation(first_line: dict[str, object]) -> str:
    """Fingerprint label for a first line that is not a session_meta envelope."""
    if "record_type" in first_line or ("payload" not in first_line and "id" in first_line):
        return "bare-items"
    return "unknown"


def scan_rollout_file(path: Path, relative_path: str) -> FileScan:
    """Scan one rollout file, applying every rule from spec sections 3, 5, 6.

    Never raises on malformed content: broken files come back with a status
    the adapter can count. Only I/O errors propagate.
    """
    with path.open("rb") as handle:
        raw = handle.read()
    raw_lines = raw.split(b"\n")
    non_blank = [(index, line) for index, line in enumerate(raw_lines) if line.strip()]
    if not non_blank:
        return FileScan(relative_path=relative_path, status=FileStatus.EMPTY, generation="empty")

    last_index = non_blank[-1][0]
    parsed: list[tuple[int, dict[str, object]]] = []
    truncated_tail = False
    for position, (index, raw_line) in enumerate(non_blank):
        try:
            loaded: object = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if index == last_index:
                # A live session file may end mid-write; the tail is dropped.
                if position == 0:
                    return FileScan(
                        relative_path=relative_path,
                        status=FileStatus.EMPTY,
                        generation="empty",
                        truncated_tail=True,
                    )
                truncated_tail = True
                break
            return FileScan(
                relative_path=relative_path, status=FileStatus.MALFORMED, generation="malformed"
            )
        if not isinstance(loaded, dict):
            if position == 0:
                return FileScan(
                    relative_path=relative_path,
                    status=FileStatus.UNSUPPORTED,
                    generation="unknown",
                )
            continue
        parsed.append((index, loaded))

    first_line = parsed[0][1]
    first_payload = first_line.get("payload")
    if first_line.get("type") != "session_meta" or not isinstance(first_payload, dict):
        return FileScan(
            relative_path=relative_path,
            status=FileStatus.UNSUPPORTED,
            generation=_unsupported_generation(first_line),
        )

    metas: list[dict[str, object]] = []
    for _, line in parsed:
        if line.get("type") == "session_meta":
            payload = line.get("payload")
            if isinstance(payload, dict):
                metas.append(payload)

    for meta in metas:
        mode = meta.get("history_mode")
        if mode is not None and mode not in _SUPPORTED_HISTORY_MODES:
            return FileScan(
                relative_path=relative_path,
                status=FileStatus.UNSUPPORTED,
                generation="unknown-history-mode",
            )

    eligible = True
    forked = False
    for meta in metas:
        if meta.get("forked_from_id") is not None:
            forked = True
        if meta.get("parent_thread_id") is not None:
            eligible = False
        if any(meta.get(key) is not None for key in _SUBAGENT_MARKER_KEYS):
            eligible = False
        for provenance_key in ("source", "thread_source"):
            provenance = meta.get(provenance_key)
            if isinstance(provenance, dict | list) and _contains_subagent(provenance):
                eligible = False

    cli_versions: list[str] = []
    for meta in metas:
        version = meta.get("cli_version")
        if isinstance(version, str) and version not in cli_versions:
            cli_versions.append(version)

    first_meta = metas[0]
    meta_id = first_meta.get("id")
    session_id = meta_id if isinstance(meta_id, str) and meta_id else None
    file_uuid = filename_session_uuid(path.name)
    session_id_mismatch = False
    if session_id is None:
        session_id = file_uuid
    elif file_uuid is not None and session_id != file_uuid:
        # Spec 5.3: on disagreement the filename UUID wins, with a diagnostic.
        session_id_mismatch = True
        session_id = file_uuid
    if session_id is None:
        return FileScan(
            relative_path=relative_path,
            status=FileStatus.UNSUPPORTED,
            generation="missing-session-id",
        )

    meta_timestamp = _parse_timestamp(first_meta.get("timestamp"))

    channel_a: list[tuple[int, dict[str, object], datetime | None]] = []
    channel_b: list[tuple[int, str, datetime | None]] = []
    channel_b_texts: set[str] = set()
    for index, line in parsed:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        timestamp = _parse_timestamp(line.get("timestamp"))
        if timestamp is None:
            timestamp = meta_timestamp
        line_type = line.get("type")
        if line_type == "event_msg" and payload.get("type") == "user_message":
            channel_a.append((index, payload, timestamp))
        elif (
            line_type == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            text = _joined_input_text(payload)
            if text:
                channel_b_texts.add(text)
                channel_b.append((index, text, timestamp))

    channel: str | None = None
    pre_filter_count = 0
    candidates: list[Candidate] = []
    channel_mismatch = False
    if channel_a:
        channel = "A"
        pre_filter_count = len(channel_a)
    elif channel_b:
        channel = "B"
        pre_filter_count = len(channel_b)

    if eligible and channel == "A":
        for index, payload, timestamp in channel_a:
            kind = payload.get("kind")
            if kind is not None and kind != "plain":
                continue
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                continue
            if _is_injected(message):
                continue
            if kind == "plain":
                confidence, basis = _CONFIDENCE_KIND_PLAIN, _BASIS_KIND_PLAIN
            else:
                confidence, basis = _CONFIDENCE_NO_KIND, _BASIS_NO_KIND
            candidates.append(Candidate(index, message, timestamp, confidence, basis))
        if channel_b_texts and any(
            candidate.text not in channel_b_texts for candidate in candidates
        ):
            channel_mismatch = True
    elif eligible and channel == "B":
        for index, text, timestamp in channel_b:
            if not text.strip() or _is_injected(text):
                continue
            candidates.append(
                Candidate(index, text, timestamp, _CONFIDENCE_CHANNEL_B, _BASIS_CHANNEL_B)
            )

    if channel == "A":
        generation = "legacy-events"
    elif channel == "B":
        generation = (
            "paginated" if first_meta.get("history_mode") == "paginated" else "response-items"
        )
    else:
        generation = "meta-only"

    return FileScan(
        relative_path=relative_path,
        status=FileStatus.SUPPORTED,
        generation=generation,
        session_id=session_id,
        eligible=eligible,
        truncated_tail=truncated_tail,
        channel=channel,
        channel_mismatch=channel_mismatch,
        forked=forked,
        session_id_mismatch=session_id_mismatch,
        cli_versions=tuple(cli_versions),
        pre_filter_count=pre_filter_count,
        candidates=tuple(candidates),
    )
