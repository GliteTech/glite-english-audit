"""Cursor global-store access and G4 record classification.

Implements the storage-generation detection, composer eligibility, and bubble
inclusion rules from ``specifications/sources/cursor.md`` (sections 2, 4, and
6) against one ``state.vscdb`` SQLite key-value store. Every read goes through
:class:`StateDatabase`, which enforces the section 3 key allowlist before
executing any SQL and reports each access to an audit callback. Aggregates
only: no function here returns, prints, or stores source text.
"""

import json
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from glite_english_audit.normalization.tokenizer import count_words

COMPOSER_PREFIX = "composerData:"
BUBBLE_PREFIX = "bubbleId:"
ALLOWED_KV_PREFIXES: tuple[str, ...] = (COMPOSER_PREFIX, BUBBLE_PREFIX)

GLOBAL_HEADERS_KEY = "composer.composerHeaders"
WORKSPACE_INDEX_KEY = "composer.composerData"
LEGACY_AICHAT_KEY = "workbench.panel.aichat.view.aichat.chatdata"
# Keys whose values may be read (spec section 3, items 1 and 2).
ALLOWED_ITEM_VALUE_KEYS: frozenset[str] = frozenset({GLOBAL_HEADERS_KEY, WORKSPACE_INDEX_KEY})
# Keys probed for existence only; their values are never read (spec 8.1.6).
ALLOWED_ITEM_PRESENCE_KEYS: frozenset[str] = frozenset({LEGACY_AICHAT_KEY})

REQUIRED_TABLES: frozenset[str] = frozenset({"ItemTable", "cursorDiskKV"})

# Spec 4.1/4.2: structurally verified revisions. Anything else fails closed.
SUPPORTED_COMPOSER_VERSIONS = range(10, 17)
SUPPORTED_BUBBLE_VERSION = 3
BUBBLE_TYPE_USER = 1

# Mirrors the claude_code adapter: prompts longer than this carry a
# possible-paste flag so normalization can quarantine them (spec 6.2).
PASTE_LENGTH_THRESHOLD = 2000

# Spec 8.4: these tags in bubble text would mean request-context leaked into
# the composer buffer, i.e. a schema change; the instance fails closed.
WRAPPER_LEAK_MARKERS: tuple[str, ...] = (
    "<user_query",
    "<attached",
    "<additional_data",
    "<custom_instructions",
    "<environment",
)

_BUSY_TIMEOUT_MS = 5000


class ComposerClass(StrEnum):
    """Generation classification of one ``composerData:`` row (spec 2, 6.1)."""

    G4 = "g4"
    G3 = "g3"
    UNSUPPORTED_VERSION = "unsupported_version"
    ID_MISMATCH = "id_mismatch"
    MALFORMED = "malformed"


class BubbleOutcome(StrEnum):
    """Classification of one ``bubbleId:`` row against the spec 6.2 rules."""

    KEPT = "kept"
    MALFORMED = "malformed"
    UNSUPPORTED_VERSION = "unsupported_version"
    EXCLUDED_ROLE = "excluded_role"
    EXCLUDED_NUDGE = "excluded_nudge"
    EXCLUDED_QUICK_SEARCH = "excluded_quick_search"
    EXCLUDED_SKIP_RENDERING = "excluded_skip_rendering"
    EXCLUDED_EMPTY = "excluded_empty"
    WRAPPER_LEAK = "wrapper_leak"


class StateDatabase:
    """Read-only, allowlist-enforced access to one ``state.vscdb``.

    Every method asserts its key or prefix against the section 3 allowlist
    before executing SQL and reports the access to the audit callback, so the
    adapter's verify() step can prove no denylisted key was ever touched.
    """

    def __init__(self, connection: sqlite3.Connection, audit: Callable[[str, str], None]) -> None:
        self._connection = connection
        self._audit = audit

    @classmethod
    def open_readonly(cls, path: Path, audit: Callable[[str, str], None]) -> "StateDatabase":
        """Open with a ``mode=ro`` URI; never creates or locks the store."""
        quoted = urllib.parse.quote(path.resolve().as_posix(), safe="/")
        connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
        try:
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        except sqlite3.Error:
            connection.close()
            raise
        return cls(connection, audit)

    def close(self) -> None:
        self._connection.close()

    def table_names(self) -> frozenset[str]:
        rows = self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return frozenset(str(row[0]) for row in rows)

    def kv_items(self, prefix: str) -> Iterator[tuple[str, str]]:
        """Key-range scan over one allowlisted ``cursorDiskKV`` prefix."""
        if prefix not in ALLOWED_KV_PREFIXES:
            msg = f"refusing to scan a cursorDiskKV prefix outside the allowlist: {prefix!r}"
            raise PermissionError(msg)
        self._audit("kv_range", prefix)
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        cursor = self._connection.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY key",
            (prefix, upper),
        )
        for key, value in cursor:
            yield str(key), _decode(value)

    def kv_value(self, key: str) -> str | None:
        """Exact-key point lookup, restricted to allowlisted prefixes."""
        if not key.startswith(ALLOWED_KV_PREFIXES):
            msg = f"refusing to read a cursorDiskKV key outside the allowlist: {key!r}"
            raise PermissionError(msg)
        self._audit("kv_get", key)
        row = self._connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return _decode(row[0])

    def item_value(self, key: str) -> str | None:
        """Read one allowlisted ``ItemTable`` row by exact key."""
        if key not in ALLOWED_ITEM_VALUE_KEYS:
            msg = f"refusing to read an ItemTable key outside the allowlist: {key!r}"
            raise PermissionError(msg)
        self._audit("item_get", key)
        row = self._connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return _decode(row[0])

    def item_present(self, key: str) -> bool:
        """Existence probe by exact key; the value is never read."""
        if key not in ALLOWED_ITEM_PRESENCE_KEYS:
            msg = f"refusing to probe an ItemTable key outside the allowlist: {key!r}"
            raise PermissionError(msg)
        self._audit("item_present", key)
        row = self._connection.execute(
            "SELECT COUNT(*) FROM ItemTable WHERE key = ?", (key,)
        ).fetchone()
        return bool(row is not None and row[0])


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def parse_epoch_ms(value: object) -> datetime | None:
    """Epoch-millisecond timestamps (composer ``createdAt``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def parse_iso_timestamp(value: object) -> datetime | None:
    """ISO 8601 timestamps (bubble ``createdAt``); unparsable becomes None."""
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
class ComposerRecord:
    """One parsed ``composerData:`` row (identity and structure, no text)."""

    composer_id: str
    classification: ComposerClass
    version: int | None = None
    created_at: datetime | None = None
    best_of_n_sub: bool = False
    sub_composer_ids: tuple[str, ...] = ()
    user_bubble_ids: tuple[str, ...] = ()


def parse_composer(key: str, raw: str) -> ComposerRecord:
    """Classify one composer row per spec 4.1 and 6.1."""
    key_id = key[len(COMPOSER_PREFIX) :]
    try:
        payload = json.loads(raw)
    except ValueError:
        return ComposerRecord(composer_id=key_id, classification=ComposerClass.MALFORMED)
    if not isinstance(payload, dict):
        return ComposerRecord(composer_id=key_id, classification=ComposerClass.MALFORMED)
    if payload.get("composerId") != key_id:
        return ComposerRecord(composer_id=key_id, classification=ComposerClass.ID_MISMATCH)
    version = payload.get("_v")
    if isinstance(version, bool) or not isinstance(version, int):
        return ComposerRecord(composer_id=key_id, classification=ComposerClass.MALFORMED)

    conversation_map = payload.get("conversationMap")
    has_embedded_conversation = isinstance(conversation_map, dict) and bool(conversation_map)
    if has_embedded_conversation or isinstance(payload.get("conversation"), list):
        return ComposerRecord(composer_id=key_id, classification=ComposerClass.G3, version=version)
    if version not in SUPPORTED_COMPOSER_VERSIONS:
        return ComposerRecord(
            composer_id=key_id,
            classification=ComposerClass.UNSUPPORTED_VERSION,
            version=version,
        )

    user_bubble_ids: list[str] = []
    headers = payload.get("fullConversationHeadersOnly")
    if isinstance(headers, list):
        for entry in headers:
            if not isinstance(entry, dict) or entry.get("type") != BUBBLE_TYPE_USER:
                continue
            bubble_id = entry.get("bubbleId")
            if isinstance(bubble_id, str):
                user_bubble_ids.append(bubble_id)
    raw_sub_ids = payload.get("subComposerIds")
    sub_ids = (
        tuple(item for item in raw_sub_ids if isinstance(item, str))
        if isinstance(raw_sub_ids, list)
        else ()
    )
    return ComposerRecord(
        composer_id=key_id,
        classification=ComposerClass.G4,
        version=version,
        created_at=parse_epoch_ms(payload.get("createdAt")),
        best_of_n_sub=payload.get("isBestOfNSubcomposer") is True,
        sub_composer_ids=sub_ids,
        user_bubble_ids=tuple(user_bubble_ids),
    )


@dataclass(frozen=True)
class BubbleRecord:
    """One parsed ``bubbleId:`` row. ``text`` is dropped after aggregation."""

    outcome: BubbleOutcome
    version: int | None = None
    text: str = ""
    created_at: datetime | None = None
    possible_paste: bool = False
    fidelity: str = "unchecked"


def _collect_lexical_texts(node: object, found: list[str]) -> None:
    if not isinstance(node, dict):
        return
    text = node.get("text")
    if isinstance(text, str):
        found.append(text)
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            _collect_lexical_texts(child, found)


def check_richtext_fidelity(text: str, rich_text: object) -> str:
    """Spec 8.4 cross-check: ``text`` against the Lexical tree's text nodes.

    Returns ``match``, ``mismatch``, or ``unchecked``. Whitespace-normalized
    equality or containment counts as a match (mention chips and code fences
    serialize differently, spec section 5).
    """
    if not isinstance(rich_text, str) or not rich_text.strip():
        return "unchecked"
    try:
        tree = json.loads(rich_text)
    except ValueError:
        return "unchecked"
    if not isinstance(tree, dict):
        return "unchecked"
    nodes: list[str] = []
    _collect_lexical_texts(tree.get("root"), nodes)
    joined = " ".join(" ".join(nodes).split())
    plain = " ".join(text.split())
    if not joined:
        return "mismatch"
    return "match" if (plain == joined or plain in joined) else "mismatch"


def parse_bubble(raw: str) -> BubbleRecord:
    """Classify one bubble row per spec 6.2, in rule order, fail closed."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return BubbleRecord(outcome=BubbleOutcome.MALFORMED)
    if not isinstance(payload, dict):
        return BubbleRecord(outcome=BubbleOutcome.MALFORMED)
    version = payload.get("_v")
    if isinstance(version, bool) or not isinstance(version, int):
        return BubbleRecord(outcome=BubbleOutcome.MALFORMED)
    if version != SUPPORTED_BUBBLE_VERSION:
        return BubbleRecord(outcome=BubbleOutcome.UNSUPPORTED_VERSION, version=version)
    if payload.get("type") != BUBBLE_TYPE_USER:
        return BubbleRecord(outcome=BubbleOutcome.EXCLUDED_ROLE, version=version)
    if payload.get("isNudge") is True:
        return BubbleRecord(outcome=BubbleOutcome.EXCLUDED_NUDGE, version=version)
    if payload.get("isQuickSearchQuery") is True:
        return BubbleRecord(outcome=BubbleOutcome.EXCLUDED_QUICK_SEARCH, version=version)
    if payload.get("skipRendering") is True:
        return BubbleRecord(outcome=BubbleOutcome.EXCLUDED_SKIP_RENDERING, version=version)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return BubbleRecord(outcome=BubbleOutcome.EXCLUDED_EMPTY, version=version)
    if text.lstrip().startswith(WRAPPER_LEAK_MARKERS):
        return BubbleRecord(outcome=BubbleOutcome.WRAPPER_LEAK, version=version)
    return BubbleRecord(
        outcome=BubbleOutcome.KEPT,
        version=version,
        text=text,
        created_at=parse_iso_timestamp(payload.get("createdAt")),
        possible_paste=len(text) > PASTE_LENGTH_THRESHOLD,
        fidelity=check_richtext_fidelity(text, payload.get("richText")),
    )


@dataclass
class GlobalStoreScan:
    """Aggregate result of scanning one global store. Counts only, no text."""

    composers_total: int = 0
    composers_g4: int = 0
    composers_g4_eligible: int = 0
    composers_g3: int = 0
    composers_unsupported_version: int = 0
    composers_id_mismatch: int = 0
    composers_malformed: int = 0
    composers_missing_bubbles: int = 0
    excluded_best_of_n: int = 0
    excluded_sub_composer: int = 0
    bubbles_referenced: int = 0
    bubbles_fetched: int = 0
    bubbles_missing: int = 0
    bubbles_malformed: int = 0
    bubbles_unsupported_version: int = 0
    bubbles_kept: int = 0
    excluded_role: int = 0
    excluded_nudge: int = 0
    excluded_quick_search: int = 0
    excluded_skip_rendering: int = 0
    excluded_empty: int = 0
    wrapper_leaks: int = 0
    fidelity_checked: int = 0
    fidelity_mismatches: int = 0
    possible_paste: int = 0
    candidate_messages: int = 0
    candidate_words: int = 0
    candidate_bytes: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    composer_versions: set[int] = field(default_factory=set)
    bubble_versions: set[int] = field(default_factory=set)

    @property
    def malformed_bubbles_over_threshold(self) -> bool:
        """Spec 9: malformed JSON in more than 10% of sampled bubble rows."""
        return self.bubbles_fetched > 0 and self.bubbles_malformed * 10 > self.bubbles_fetched

    @property
    def unsupported_bubbles_dominate(self) -> bool:
        """Spec 6.2.2: unknown bubble revisions dominating the instance."""
        return (
            self.bubbles_fetched > 0 and self.bubbles_unsupported_version * 2 > self.bubbles_fetched
        )


_BUBBLE_OUTCOME_COUNTERS: dict[BubbleOutcome, str] = {
    BubbleOutcome.MALFORMED: "bubbles_malformed",
    BubbleOutcome.UNSUPPORTED_VERSION: "bubbles_unsupported_version",
    BubbleOutcome.EXCLUDED_ROLE: "excluded_role",
    BubbleOutcome.EXCLUDED_NUDGE: "excluded_nudge",
    BubbleOutcome.EXCLUDED_QUICK_SEARCH: "excluded_quick_search",
    BubbleOutcome.EXCLUDED_SKIP_RENDERING: "excluded_skip_rendering",
    BubbleOutcome.EXCLUDED_EMPTY: "excluded_empty",
    BubbleOutcome.WRAPPER_LEAK: "wrapper_leaks",
}


def _scan_composer_bubbles(
    db: StateDatabase, composer: ComposerRecord, scan: GlobalStoreScan
) -> None:
    references = composer.user_bubble_ids
    scan.bubbles_referenced += len(references)
    fetched: list[BubbleRecord] = []
    missing = 0
    for bubble_id in references:
        raw = db.kv_value(f"{BUBBLE_PREFIX}{composer.composer_id}:{bubble_id}")
        if raw is None:
            missing += 1
            continue
        fetched.append(parse_bubble(raw))
    scan.bubbles_missing += missing
    scan.bubbles_fetched += len(fetched)
    for record in fetched:
        if record.version is not None:
            scan.bubble_versions.add(record.version)
        counter = _BUBBLE_OUTCOME_COUNTERS.get(record.outcome)
        if counter is not None:
            setattr(scan, counter, getattr(scan, counter) + 1)
    if references and missing * 10 > len(references):
        # Spec 9: over 10% of this composer's user-bubble references are
        # missing (possible mid-migration store); its candidates are dropped.
        scan.composers_missing_bubbles += 1
        return
    scan.composers_g4_eligible += 1
    for record in fetched:
        if record.outcome is not BubbleOutcome.KEPT:
            continue
        scan.bubbles_kept += 1
        scan.candidate_messages += 1
        scan.candidate_words += count_words(record.text)
        scan.candidate_bytes += len(record.text.encode("utf-8"))
        if record.possible_paste:
            scan.possible_paste += 1
        if record.fidelity != "unchecked":
            scan.fidelity_checked += 1
            if record.fidelity == "mismatch":
                scan.fidelity_mismatches += 1
        timestamp = record.created_at or composer.created_at
        if timestamp is not None:
            if scan.earliest is None or timestamp < scan.earliest:
                scan.earliest = timestamp
            if scan.latest is None or timestamp > scan.latest:
                scan.latest = timestamp


def scan_global_store(db: StateDatabase) -> GlobalStoreScan:
    """Scan one global store per spec 8.1: composers first, then only the
    ``type == 1`` bubble rows by exact key, never the assistant rows."""
    scan = GlobalStoreScan()
    composers = [parse_composer(key, raw) for key, raw in db.kv_items(COMPOSER_PREFIX)]
    known_sub_ids = {sub_id for composer in composers for sub_id in composer.sub_composer_ids}
    for composer in composers:
        scan.composers_total += 1
        if composer.classification is ComposerClass.MALFORMED:
            scan.composers_malformed += 1
            continue
        if composer.classification is ComposerClass.ID_MISMATCH:
            scan.composers_id_mismatch += 1
            continue
        if composer.classification is ComposerClass.G3:
            scan.composers_g3 += 1
            continue
        if composer.classification is ComposerClass.UNSUPPORTED_VERSION:
            scan.composers_unsupported_version += 1
            if composer.version is not None:
                scan.composer_versions.add(composer.version)
            continue
        scan.composers_g4 += 1
        if composer.version is not None:
            scan.composer_versions.add(composer.version)
        if composer.best_of_n_sub:
            scan.excluded_best_of_n += 1
            continue
        if composer.composer_id in known_sub_ids:
            scan.excluded_sub_composer += 1
            continue
        _scan_composer_bubbles(db, composer, scan)
    return scan


def parse_workspace_index(raw: str | None) -> tuple[tuple[str, ...], bool]:
    """Composer IDs from a workspace ``composer.composerData`` row.

    Returns the linked composer UUIDs and whether any entry carries inline
    conversation content (the G2 legacy marker, spec 2). Only ``composerId``
    values are taken from the row (spec 4.3).
    """
    if raw is None:
        return (), False
    try:
        payload = json.loads(raw)
    except ValueError:
        return (), False
    if not isinstance(payload, dict):
        return (), False
    entries = payload.get("allComposers")
    if not isinstance(entries, list):
        return (), False
    composer_ids: list[str] = []
    inline_conversation = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        composer_id = entry.get("composerId")
        if isinstance(composer_id, str):
            composer_ids.append(composer_id)
        for legacy_field in ("conversation", "messages"):
            value = entry.get(legacy_field)
            if isinstance(value, list) and value:
                inline_conversation = True
    return tuple(composer_ids), inline_conversation
