"""The Aider source adapter.

Discovers per-project history-file instances by scanning the configured
search roots for the two exact Aider history filenames (plus the two env-var
overrides), snapshots the selected channel file byte-for-byte, and extracts
candidate user-authored utterances per ``specifications/sources/aider.md``.

The primary channel is ``.aider.input.history`` (spec 4.7); the chat
Markdown transcript is fallback only, and the two channels are never merged
for one instance. The adapter opens nothing but the selected history files:
config files, ``.env`` files, the LLM traffic log, and everything under
``~/.aider/`` are denylisted (spec section 3) and checked before every open.
Discovery never returns, prints, or logs source text. Timestamps are naive
local time (timezone unknown) exactly as Aider stores them.
"""

import hashlib
import json
import os
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from glite_english_audit.adapters.aider.records import (
    ChatMarkdownScan,
    InputHistoryScan,
    scan_chat_markdown,
    scan_input_history,
)
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.models import (
    NormalizedUtterance,
    SnapshotFileEntry,
    SourceInstanceRecord,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery.base import (
    DiscoveryContext,
    DiscoveryOutcome,
    SnapshotCapture,
    SourceAdapter,
)
from glite_english_audit.discovery.scan_exclusions import (
    audit_owned_roots,
    should_prune_scan_dir,
)
from glite_english_audit.normalization.tokenizer import count_words

ADAPTER_ID = "aider"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Aider"
_STORAGE_FORMAT = "text"
INPUT_HISTORY_NAME = ".aider.input.history"
CHAT_MARKDOWN_NAME = ".aider.chat.history.md"
INPUT_HISTORY_ENV = "AIDER_INPUT_HISTORY_FILE"
CHAT_MARKDOWN_ENV = "AIDER_CHAT_HISTORY_FILE"
_SNAPSHOT_META_NAME = "aider-source-meta.json"

# Spec 2.3: the filename scan descends at most this many levels below a root.
DEFAULT_SCAN_DEPTH = 6

# Spec section 3: every other file Aider writes must never be opened. The
# allowlist assertion before each open is the primary guard; this denylist is
# checked again as defense in depth.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".aider.conf.yml",
        ".env",
        ".aider.llm.history",
        ".aider.model.settings.yml",
        ".aider.model.metadata.json",
        "oauth-keys.env",
        "analytics.json",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset({".aider"})
_DENY_DIR_PREFIX = ".aider.tags.cache"

# Spec 2.3: directories pruned during the filename scan.
_PRUNE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".cache",
        "Library",
        "AppData",
    }
)

_CHANNEL_INPUT = "input-history"
_CHANNEL_CHAT = "chat-markdown"
_CHANNEL_NONE = "none"

# Naive sentinel: every Aider timestamp is timezone-unknown local time.
_MAX_TIMESTAMP = datetime.max


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _copy_bytes(source: Path, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    return digest.hexdigest(), size


def _restrict_mode(path: Path, mode: int) -> None:
    """Spec 8.2: snapshot copies are 0600 files in 0700 directories (POSIX)."""
    if os.name == "posix":
        path.chmod(mode)


def _fingerprint(*, base: str, line_ending: str, parse_failures: int, degraded: bool) -> str:
    """Spec 8.1: variant plus line-ending style plus timestamp-parse health."""
    parts = [base, line_ending]
    if parse_failures:
        parts.append("ts-parse-errors")
    if degraded:
        parts.append("ts-degraded")
    return ";".join(parts)


@dataclass(frozen=True)
class _ChannelSelection:
    """The one extraction channel chosen for an instance (spec 2.3 step 4)."""

    channel: str
    file_path: Path | None


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    channel: str
    truncated_tail: bool
    timestamp_parse_failures: int


@dataclass
class _InstanceFiles:
    """The history files found in one instance directory."""

    root: Path
    input_history: Path | None = None
    chat_markdown: Path | None = None


@dataclass
class _ProvisionalInstance:
    """One instance directory's aggregates before label assignment."""

    root: Path
    path_hash: str
    accessibility: Accessibility
    diagnostic_code: str | None
    schema_fingerprint: str
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    messages: int
    words: int
    bytes_count: int


class AiderAdapter:
    """SourceAdapter implementation for Aider's per-project history files."""

    def __init__(
        self,
        extra_scan_roots: Sequence[Path] = (),
        max_scan_depth: int = DEFAULT_SCAN_DEPTH,
        audit_roots: frozenset[Path] | None = None,
    ) -> None:
        self._extra_scan_roots = tuple(extra_scan_roots)
        self._max_scan_depth = max_scan_depth
        self._audit_roots = audit_roots if audit_roots is not None else audit_owned_roots()
        self._opened_paths: list[Path] = []
        self._env_allowed: set[str] = set()
        self._channels: dict[str, _ChannelSelection] = {}
        self._extraction_stats: dict[str, ExtractionStats] = {}

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        return Stability.STABLE

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        instances = self._collect_instances(context)
        if not instances:
            return self._not_found_outcome(context)
        provisional = [self._scan_instance(files) for files in instances]
        return self._label_and_build(context, provisional)

    def _collect_instances(self, context: DiscoveryContext) -> list[_InstanceFiles]:
        by_root: dict[str, _InstanceFiles] = {}
        claimed: set[str] = set()

        def entry(directory: Path) -> _InstanceFiles:
            key = _canonical_path(directory)
            if key not in by_root:
                by_root[key] = _InstanceFiles(root=Path(key))
            return by_root[key]

        # Spec 2.3 step 1: environment overrides; a missing path is ignored
        # silently, a denylisted target is refused without being opened.
        for env_name, is_input in ((INPUT_HISTORY_ENV, True), (CHAT_MARKDOWN_ENV, False)):
            raw = context.environ.get(env_name, "").strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            if not candidate.is_file() or self._is_denylisted(candidate):
                continue
            resolved = Path(_canonical_path(candidate))
            self._env_allowed.add(str(resolved))
            claimed.add(str(resolved))
            files = entry(resolved.parent)
            if is_input:
                files.input_history = resolved
            else:
                files.chat_markdown = resolved

        # Spec 2.3 step 2: filename-pattern scan of the search roots. The
        # canonical file path collapses symlinked or doubly-reachable files.
        for root in (context.home, *self._extra_scan_roots):
            for directory, names in self._walk(root, context.os_environment):
                for name in names:
                    canonical_file = _canonical_path(directory / name)
                    if canonical_file in claimed:
                        continue
                    claimed.add(canonical_file)
                    files = entry(directory)
                    if name == INPUT_HISTORY_NAME and files.input_history is None:
                        files.input_history = Path(canonical_file)
                    elif name == CHAT_MARKDOWN_NAME and files.chat_markdown is None:
                        files.chat_markdown = Path(canonical_file)
        return list(by_root.values())

    def _walk(self, root: Path, os_environment: OsEnvironment) -> Iterator[tuple[Path, list[str]]]:
        if not root.is_dir() or self._pruned_directory(root, os_environment):
            return
        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        while queue:
            directory, depth = queue.popleft()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            names = [
                item.name
                for item in entries
                if item.name in (INPUT_HISTORY_NAME, CHAT_MARKDOWN_NAME) and item.is_file()
            ]
            if names:
                yield directory, names
            if depth >= self._max_scan_depth:
                continue
            for item in entries:
                if item.is_symlink() or not item.is_dir():
                    continue
                if self._pruned_directory(item, os_environment):
                    continue
                queue.append((item, depth + 1))

    def _pruned_directory(self, directory: Path, os_environment: OsEnvironment) -> bool:
        name = directory.name
        if name in _PRUNE_DIR_NAMES or name in DENY_DIR_NAMES:
            return True
        if should_prune_scan_dir(directory, audit_roots=self._audit_roots):
            return True
        if name.startswith(_DENY_DIR_PREFIX):
            return True
        if (
            name == "Trash"
            and directory.parent.name == "share"
            and directory.parent.parent.name == ".local"
        ):
            return True
        if os_environment is OsEnvironment.WSL and str(directory) == "/mnt":
            return True
        return os.path.ismount(directory)

    def _not_found_outcome(self, context: DiscoveryContext) -> DiscoveryOutcome:
        root = context.home
        path_hash = _hash_text(_canonical_path(root))
        record = SourceInstanceRecord(
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            instance_key=path_hash,
            opaque_label=f"{_HUMAN_NAME} 1",
            storage_format=_STORAGE_FORMAT,
            schema_fingerprint="absent",
            path_hash=path_hash,
            os_environment=context.os_environment,
            app_version=None,
            stability=Stability.STABLE,
            accessibility=Accessibility.NOT_FOUND,
            diagnostic_code="SOURCE_NOT_FOUND",
            estimated_records=0,
            earliest_timestamp=None,
            latest_timestamp=None,
            candidate_messages=0,
            candidate_words=0,
            candidate_bytes=0,
        )
        return DiscoveryOutcome(records=[record], instance_paths={path_hash: root})

    def _choose_channel(
        self, files: _InstanceFiles
    ) -> tuple[_ChannelSelection, InputHistoryScan | None, ChatMarkdownScan | None, bool]:
        """Spec 2.3 step 4: input history wins; chat markdown is fallback."""
        input_scan: InputHistoryScan | None = None
        chat_scan: ChatMarkdownScan | None = None
        had_error = False
        if files.input_history is not None:
            try:
                input_scan = self._scan_input(files.input_history)
            except OSError:
                had_error = True
        if input_scan is not None and not input_scan.unsupported:
            selection = _ChannelSelection(channel=_CHANNEL_INPUT, file_path=files.input_history)
            return selection, input_scan, None, had_error
        if files.chat_markdown is not None:
            try:
                chat_scan = self._scan_chat(files.chat_markdown)
            except OSError:
                had_error = True
        if chat_scan is not None and not chat_scan.unsupported:
            selection = _ChannelSelection(channel=_CHANNEL_CHAT, file_path=files.chat_markdown)
            return selection, input_scan, chat_scan, had_error
        none_selection = _ChannelSelection(channel=_CHANNEL_NONE, file_path=None)
        return none_selection, input_scan, chat_scan, had_error

    def _scan_instance(self, files: _InstanceFiles) -> _ProvisionalInstance:
        path_hash = _hash_text(_canonical_path(files.root))
        selection, input_scan, chat_scan, had_error = self._choose_channel(files)
        self._channels[path_hash] = selection
        if selection.channel == _CHANNEL_INPUT and input_scan is not None:
            return self._build_from_input(files.root, path_hash, input_scan)
        if selection.channel == _CHANNEL_CHAT and chat_scan is not None:
            fallback = files.input_history is not None
            return self._build_from_chat(files.root, path_hash, chat_scan, fallback=fallback)
        if had_error and input_scan is None and chat_scan is None:
            accessibility = Accessibility.INACCESSIBLE
            diagnostic_code = "SOURCE_INACCESSIBLE"
            fingerprint = "unknown"
        else:
            # Spec 9: any shape not matching section 4 fails closed.
            accessibility = Accessibility.UNSUPPORTED_SCHEMA
            diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"
            fingerprint = "unsupported"
        return _ProvisionalInstance(
            root=files.root,
            path_hash=path_hash,
            accessibility=accessibility,
            diagnostic_code=diagnostic_code,
            schema_fingerprint=fingerprint,
            estimated_records=0,
            earliest=None,
            latest=None,
            messages=0,
            words=0,
            bytes_count=0,
        )

    def _build_from_input(
        self, root: Path, path_hash: str, scan: InputHistoryScan
    ) -> _ProvisionalInstance:
        stamps = [entry.timestamp for entry in scan.kept if entry.timestamp is not None]
        texts = [entry.text for entry in scan.kept]
        return _ProvisionalInstance(
            root=root,
            path_hash=path_hash,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=_fingerprint(
                base="input-history-v1",
                line_ending=scan.line_ending,
                parse_failures=scan.timestamp_parse_failures,
                degraded=scan.timestamp_degraded,
            ),
            estimated_records=scan.entry_count,
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            messages=len(scan.kept),
            words=sum(count_words(text) for text in texts),
            bytes_count=sum(len(text.encode("utf-8")) for text in texts),
        )

    def _build_from_chat(
        self, root: Path, path_hash: str, scan: ChatMarkdownScan, *, fallback: bool
    ) -> _ProvisionalInstance:
        stamps = [message.timestamp for message in scan.kept if message.timestamp is not None]
        texts = [message.text for message in scan.kept]
        base = "chat-markdown-v1-fallback" if fallback else "chat-markdown-v1"
        return _ProvisionalInstance(
            root=root,
            path_hash=path_hash,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=_fingerprint(
                base=base,
                line_ending=scan.line_ending,
                parse_failures=scan.timestamp_parse_failures,
                degraded=False,
            ),
            estimated_records=scan.formed_messages,
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            messages=len(scan.kept),
            words=sum(count_words(text) for text in texts),
            bytes_count=sum(len(text.encode("utf-8")) for text in texts),
        )

    def _label_and_build(
        self, context: DiscoveryContext, provisional: list[_ProvisionalInstance]
    ) -> DiscoveryOutcome:
        ordered = sorted(
            provisional, key=lambda item: (item.earliest or _MAX_TIMESTAMP, item.path_hash)
        )
        records: list[SourceInstanceRecord] = []
        instance_paths: dict[str, Path] = {}
        for position, item in enumerate(ordered, start=1):
            records.append(
                SourceInstanceRecord(
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    instance_key=item.path_hash,
                    opaque_label=f"{_HUMAN_NAME} {position}",
                    storage_format=_STORAGE_FORMAT,
                    schema_fingerprint=item.schema_fingerprint,
                    path_hash=item.path_hash,
                    os_environment=context.os_environment,
                    app_version=None,
                    stability=Stability.STABLE,
                    accessibility=item.accessibility,
                    diagnostic_code=item.diagnostic_code,
                    estimated_records=item.estimated_records,
                    earliest_timestamp=item.earliest,
                    latest_timestamp=item.latest,
                    candidate_messages=item.messages,
                    candidate_words=item.words,
                    candidate_bytes=item.bytes_count,
                )
            )
            instance_paths[item.path_hash] = item.root
        return DiscoveryOutcome(records=records, instance_paths=instance_paths)

    # -- file access guards ------------------------------------------------

    def _is_denylisted(self, path: Path) -> bool:
        if path.name in DENY_FILE_NAMES:
            return True
        return any(
            part in DENY_DIR_NAMES or part.startswith(_DENY_DIR_PREFIX) for part in path.parts
        )

    def _assert_openable(self, path: Path) -> None:
        if self._is_denylisted(path):
            msg = "refusing to open a denylisted Aider path"
            raise PermissionError(msg)
        if path.name in (INPUT_HISTORY_NAME, CHAT_MARKDOWN_NAME, _SNAPSHOT_META_NAME):
            return
        if _canonical_path(path) in self._env_allowed:
            return
        if (path.parent / _SNAPSHOT_META_NAME).is_file():
            # A snapshot copy of an env-relocated history file.
            return
        msg = "refusing to open a path outside the history-file allowlist"
        raise PermissionError(msg)

    def _scan_input(self, path: Path) -> InputHistoryScan:
        self._assert_openable(path)
        self._opened_paths.append(path)
        return scan_input_history(path)

    def _scan_chat(self, path: Path) -> ChatMarkdownScan:
        self._assert_openable(path)
        self._opened_paths.append(path)
        return scan_chat_markdown(path)

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        selection = self._channels.get(instance.instance_key)
        if selection is None:
            selection = self._infer_selection(source_path)
        target_dir.mkdir(parents=True, exist_ok=True)
        _restrict_mode(target_dir, 0o700)
        entries: list[SnapshotFileEntry] = []
        meta: dict[str, str] = {"channel": selection.channel}
        if selection.channel != _CHANNEL_NONE and selection.file_path is not None:
            source_file = selection.file_path
            self._assert_openable(source_file)
            self._opened_paths.append(source_file)
            target_file = target_dir / source_file.name
            digest, size = _copy_bytes(source_file, target_file)
            _restrict_mode(target_file, 0o600)
            entries.append(
                SnapshotFileEntry(relative_path=source_file.name, size_bytes=size, sha256=digest)
            )
            meta["file_name"] = source_file.name
            meta["source_path_hash"] = _hash_text(_canonical_path(source_file))
        payload = json.dumps(meta, sort_keys=True, indent=2).encode("utf-8")
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        _restrict_mode(meta_path, 0o600)
        entries.append(
            SnapshotFileEntry(
                relative_path=_SNAPSHOT_META_NAME,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    def _infer_selection(self, source_path: Path) -> _ChannelSelection:
        """Rebuild the channel choice when discovery state is unavailable."""
        if source_path.is_file():
            channel = _CHANNEL_CHAT if source_path.name == CHAT_MARKDOWN_NAME else _CHANNEL_INPUT
            return _ChannelSelection(channel=channel, file_path=source_path)
        files = _InstanceFiles(root=source_path)
        input_path = source_path / INPUT_HISTORY_NAME
        chat_path = source_path / CHAT_MARKDOWN_NAME
        if input_path.is_file() and not input_path.is_symlink():
            files.input_history = input_path
        if chat_path.is_file() and not chat_path.is_symlink():
            files.chat_markdown = chat_path
        selection, _, _, _ = self._choose_channel(files)
        return selection

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        meta = self._read_snapshot_meta(snapshot_dir)
        channel = meta.get("channel", _CHANNEL_NONE)
        file_name = meta.get("file_name")
        source_path_hash = meta.get("source_path_hash", instance.path_hash)
        if channel == _CHANNEL_NONE or file_name is None:
            self._extraction_stats[instance.instance_key] = ExtractionStats(
                utterance_count=0,
                channel=_CHANNEL_NONE,
                truncated_tail=False,
                timestamp_parse_failures=0,
            )
            return
        snapshot_file = snapshot_dir / file_name
        if channel == _CHANNEL_INPUT:
            input_scan = self._scan_input(snapshot_file)
            for entry in input_scan.kept:
                yield NormalizedUtterance(
                    utterance_id=f"{ADAPTER_ID}-{source_path_hash[:16]}-e{entry.ordinal}",
                    source_adapter=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    session_hash=source_path_hash,
                    timestamp=entry.timestamp,
                    text=entry.text,
                    modality=Modality.WRITTEN,
                    text_status=TextStatus.VERBATIM,
                    authorship_confidence=0.95,
                    authorship_basis="input_history_prompt_entry",
                    source_path_hash=source_path_hash,
                    content_flags=list(entry.content_flags),
                )
            stats = ExtractionStats(
                utterance_count=len(input_scan.kept),
                channel=channel,
                truncated_tail=input_scan.truncated_tail,
                timestamp_parse_failures=input_scan.timestamp_parse_failures,
            )
        else:
            chat_scan = self._scan_chat(snapshot_file)
            session_hashes: dict[int, str] = {}
            for message in chat_scan.kept:
                session_hash = session_hashes.setdefault(
                    message.banner_ordinal,
                    _hash_text(f"{source_path_hash}:{message.banner_ordinal}"),
                )
                yield NormalizedUtterance(
                    utterance_id=(
                        f"{ADAPTER_ID}-{session_hash[:16]}-"
                        f"b{message.banner_ordinal}m{message.message_ordinal}"
                    ),
                    source_adapter=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    session_hash=session_hash,
                    timestamp=message.timestamp,
                    text=message.text,
                    modality=Modality.WRITTEN,
                    text_status=TextStatus.VERBATIM,
                    authorship_confidence=0.7,
                    authorship_basis="chat_markdown_user_prefix",
                    source_path_hash=source_path_hash,
                    content_flags=list(message.content_flags),
                )
            stats = ExtractionStats(
                utterance_count=len(chat_scan.kept),
                channel=channel,
                truncated_tail=chat_scan.truncated_tail,
                timestamp_parse_failures=chat_scan.timestamp_parse_failures,
            )
        self._extraction_stats[instance.instance_key] = stats

    def _read_snapshot_meta(self, snapshot_dir: Path) -> dict[str, str]:
        meta_path = snapshot_dir / _SNAPSHOT_META_NAME
        if not meta_path.is_file():
            return {}
        self._opened_paths.append(meta_path)
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            key: value
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def extraction_stats(self, instance_key: str) -> ExtractionStats | None:
        """Accounting for the last extraction of one instance, if any."""
        return self._extraction_stats.get(instance_key)

    # -- verification ------------------------------------------------------

    def verify(
        self,
        instance: SourceInstanceRecord,
        utterances: list[NormalizedUtterance],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        seen_ids: set[str] = set()
        for utterance in utterances:
            if utterance.utterance_id in seen_ids:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "duplicate utterance ID in one instance extraction",
                        item_ref=utterance.utterance_id,
                    )
                )
            seen_ids.add(utterance.utterance_id)
            if utterance.source_adapter != ADAPTER_ID or not utterance.utterance_id.startswith(
                f"{ADAPTER_ID}-"
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance does not belong to the aider adapter",
                        item_ref=utterance.utterance_id,
                    )
                )
            stripped = utterance.text.strip()
            if not stripped:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text is empty",
                        item_ref=utterance.utterance_id,
                    )
                )
            elif stripped.startswith(("/", "!")):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text is a command or shell line the spec excludes",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.timestamp is None and "undated" not in utterance.content_flags:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "an utterance without a timestamp must carry the undated flag",
                        item_ref=utterance.utterance_id,
                    )
                )
            if (
                utterance.timestamp is not None
                and instance.earliest_timestamp is not None
                and instance.latest_timestamp is not None
                and not (
                    instance.earliest_timestamp <= utterance.timestamp <= instance.latest_timestamp
                )
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance timestamp is outside the instance's observed range",
                        item_ref=utterance.utterance_id,
                    )
                )
        if len(utterances) != instance.candidate_messages:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"extracted {len(utterances)} utterances but discovery counted "
                    f"{instance.candidate_messages} candidate messages",
                    item_ref=instance.instance_key,
                )
            )
        for path in self._opened_paths:
            if self._is_denylisted(path):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        "a denylisted path appears in the opened-path audit log",
                    )
                )
        return diagnostics


def create_adapter(audit_roots: frozenset[Path] | None = None) -> SourceAdapter:
    """Factory registered by the adapter coordinator.

    ``audit_roots`` overrides the project-owned directories the scan skips;
    tests use it to point the rule at a temporary repository.
    """
    return AiderAdapter(audit_roots=audit_roots)
