"""The Claude Code CLI source adapter.

Discovers per-project transcript instances under
``$CLAUDE_CONFIG_DIR/projects/`` (falling back to ``~/.claude/projects/``),
snapshots the plain JSONL session files byte-for-byte, and extracts candidate
user-authored utterances per ``specifications/sources/claude_code.md``.

The adapter opens only ``projects/<encoded-cwd>/<session>.jsonl`` files. The
single-glob allowlist is enforced before every open; the denylist check is
defense in depth (spec section 2). Discovery never returns, prints, or logs
source text.
"""

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.claude_code.records import (
    FileScan,
    KeptCandidate,
    scan_transcript,
)
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.io import ensure_private_dir, restrict_private_file
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
from glite_english_audit.discovery.parallel import map_in_processes, worker_count
from glite_english_audit.normalization.tokenizer import count_words

ADAPTER_ID = "claude_code"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Claude Code"
_STORAGE_FORMAT = "jsonl"
_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_SNAPSHOT_META_NAME = "claude-code-source-paths.json"

# Spec section 2: root entries that must never be opened. The allowlist
# (depth-2 *.jsonl under projects/) already excludes them; these names are
# checked again before every open as defense in depth.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".credentials.json",
        ".claude.json",
        "history.jsonl",
        "settings.json",
        "settings.local.json",
        "managed-settings.json",
        "stats-cache.json",
        "CLAUDE.md",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset(
    {
        "subagents",
        "tool-results",
        "shell-snapshots",
        "file-history",
        "paste-cache",
        "uploads",
        "backups",
        "plans",
        "debug",
        "telemetry",
        "session-env",
        "plugins",
    }
)

_MAX_TIMESTAMP = datetime.max.replace(tzinfo=UTC)
_EARLIEST_PLAUSIBLE = datetime(2020, 1, 1, tzinfo=UTC)

_WRAPPER_MARKERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _dedup_candidates(scans: Sequence[FileScan]) -> list[tuple[FileScan, KeptCandidate]]:
    """Fork dedup (spec 5): same uuid plus equal normalized text collapses.

    Files are visited by earliest kept timestamp, so the earliest copy wins
    and keeps the canonical attribution.
    """
    ordered = sorted(
        (scan for scan in scans if not scan.unsupported),
        key=lambda scan: (scan.earliest_kept_timestamp or _MAX_TIMESTAMP, scan.file_name),
    )
    seen: set[tuple[str, str]] = set()
    result: list[tuple[FileScan, KeptCandidate]] = []
    for scan in ordered:
        for candidate in scan.kept:
            key = (candidate.record_uuid, candidate.text_sha256)
            if key in seen:
                continue
            seen.add(key)
            result.append((scan, candidate))
    return result


def _fingerprint(scans: Sequence[FileScan]) -> str:
    supported = [scan for scan in scans if not scan.unsupported]
    if not any(scan.parsed_records for scan in supported):
        return "empty"
    legacy = any(scan.legacy_marker for scan in supported)
    current = any(scan.current_marker for scan in supported)
    if legacy and current:
        return "mixed"
    if legacy:
        return "v1-legacy"
    return "v2-current"


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    files_scanned: int
    unsupported_files: tuple[str, ...]
    truncated_tail_files: tuple[str, ...]
    malformed_lines: int
    structural_warnings: int


@dataclass
class _ProvisionalInstance:
    """One project directory's aggregates before label assignment."""

    root: Path
    path_hash: str
    accessibility: Accessibility
    diagnostic_code: str | None
    schema_fingerprint: str
    app_version: str | None
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    messages: int
    words: int
    bytes_count: int


@dataclass(frozen=True)
class _InstanceScan:
    """One project directory's aggregates plus the paths the scan opened.

    Deliberately text-free: this value is what crosses the worker-process
    boundary, so counts, timestamps, and file paths are all that can ever
    leave a worker. The path list keeps the adapter's opened-path audit
    complete when scanning runs out of process.
    """

    instance: _ProvisionalInstance
    opened_paths: tuple[str, ...]


def _list_transcript_files(directory: Path) -> list[Path]:
    """Depth-1 session transcripts only; everything else stays unread."""
    return sorted(
        entry
        for entry in directory.iterdir()
        if entry.is_file()
        and not entry.is_symlink()
        and entry.suffix == ".jsonl"
        and entry.name not in DENY_FILE_NAMES
    )


def _assert_transcript_allowed(path: Path) -> None:
    if (
        path.suffix != ".jsonl"
        or path.name in DENY_FILE_NAMES
        or any(part in DENY_DIR_NAMES for part in path.parts)
    ):
        msg = "refusing to open a path outside the transcript allowlist"
        raise PermissionError(msg)


def _unreadable_instance(directory: Path, path_hash: str) -> _ProvisionalInstance:
    return _ProvisionalInstance(
        root=directory,
        path_hash=path_hash,
        accessibility=Accessibility.INACCESSIBLE,
        diagnostic_code="SOURCE_INACCESSIBLE",
        schema_fingerprint="unknown",
        app_version=None,
        estimated_records=0,
        earliest=None,
        latest=None,
        messages=0,
        words=0,
        bytes_count=0,
    )


def _scan_project_directory(directory_path: str) -> _InstanceScan:
    """Scan one project directory down to counts. Runs in a worker process."""
    directory = Path(directory_path)
    path_hash = _hash_text(_canonical_path(directory))
    opened: list[str] = []
    try:
        scans: list[FileScan] = []
        for path in _list_transcript_files(directory):
            _assert_transcript_allowed(path)
            opened.append(str(path))
            scans.append(scan_transcript(path))
    except OSError:
        return _InstanceScan(
            instance=_unreadable_instance(directory, path_hash),
            opened_paths=tuple(opened),
        )
    app_version = next(
        (scan.app_version for scan in reversed(scans) if scan.app_version is not None),
        None,
    )
    if scans and all(scan.unsupported for scan in scans):
        return _InstanceScan(
            instance=_ProvisionalInstance(
                root=directory,
                path_hash=path_hash,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                schema_fingerprint="unsupported",
                app_version=app_version,
                estimated_records=0,
                earliest=None,
                latest=None,
                messages=0,
                words=0,
                bytes_count=0,
            ),
            opened_paths=tuple(opened),
        )
    deduped = _dedup_candidates(scans)
    stamps = [candidate.timestamp for _, candidate in deduped if candidate.timestamp is not None]
    return _InstanceScan(
        instance=_ProvisionalInstance(
            root=directory,
            path_hash=path_hash,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=_fingerprint(scans),
            app_version=app_version,
            estimated_records=sum(scan.parsed_records for scan in scans),
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            messages=len(deduped),
            words=sum(count_words(candidate.text) for _, candidate in deduped),
            bytes_count=sum(len(candidate.text.encode("utf-8")) for _, candidate in deduped),
        ),
        opened_paths=tuple(opened),
    )


class ClaudeCodeAdapter:
    """SourceAdapter implementation for the Claude Code CLI transcript store."""

    def __init__(self) -> None:
        self._opened_paths: list[Path] = []
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
        root = self._storage_root(context)
        projects = root / "projects"
        if not projects.is_dir():
            return self._degenerate_outcome(context, root)
        try:
            project_dirs = sorted(
                entry for entry in projects.iterdir() if entry.is_dir() and not entry.is_symlink()
            )
        except OSError:
            return self._single_record_outcome(
                context,
                root,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        if not project_dirs:
            return self._degenerate_outcome(context, root)

        # Project directories are independent, so they scan in parallel. The
        # results stay in directory order, which is what _label_and_build then
        # sorts: no completion order can renumber an opaque label.
        scans = map_in_processes(
            _scan_project_directory,
            [str(directory) for directory in project_dirs],
            workers=worker_count(item_count=len(project_dirs), environ=context.environ),
        )
        provisional: list[_ProvisionalInstance] = []
        for scan in scans:
            self._opened_paths.extend(Path(opened) for opened in scan.opened_paths)
            provisional.append(scan.instance)
        return self._label_and_build(context, provisional)

    def _storage_root(self, context: DiscoveryContext) -> Path:
        override = context.environ.get(_CONFIG_DIR_ENV, "").strip()
        if override:
            return Path(override)
        return context.home / ".claude"

    def _degenerate_outcome(self, context: DiscoveryContext, root: Path) -> DiscoveryOutcome:
        """The root exists without transcripts (found, empty) or is absent."""
        if root.is_dir():
            return self._single_record_outcome(
                context,
                root,
                accessibility=Accessibility.FOUND,
                diagnostic_code=None,
                fingerprint="empty",
            )
        return self._single_record_outcome(
            context,
            root,
            accessibility=Accessibility.NOT_FOUND,
            diagnostic_code="SOURCE_NOT_FOUND",
            fingerprint="absent",
        )

    def _single_record_outcome(
        self,
        context: DiscoveryContext,
        root: Path,
        *,
        accessibility: Accessibility,
        diagnostic_code: str | None,
        fingerprint: str,
    ) -> DiscoveryOutcome:
        provisional = _ProvisionalInstance(
            root=root,
            path_hash=_hash_text(_canonical_path(root)),
            accessibility=accessibility,
            diagnostic_code=diagnostic_code,
            schema_fingerprint=fingerprint,
            app_version=None,
            estimated_records=0,
            earliest=None,
            latest=None,
            messages=0,
            words=0,
            bytes_count=0,
        )
        return self._label_and_build(context, [provisional])

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
                    app_version=item.app_version,
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

    def _transcript_files(self, directory: Path) -> list[Path]:
        """Depth-1 session transcripts only; everything else stays unread."""
        return _list_transcript_files(directory)

    def _assert_readable_transcript(self, path: Path) -> None:
        _assert_transcript_allowed(path)

    def _scan_file(self, path: Path) -> FileScan:
        _assert_transcript_allowed(path)
        self._opened_paths.append(path)
        return scan_transcript(path)

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        ensure_private_dir(target_dir)
        entries: list[SnapshotFileEntry] = []
        path_hashes: dict[str, str] = {}
        for source_file in self._transcript_files(source_path):
            self._assert_readable_transcript(source_file)
            self._opened_paths.append(source_file)
            digest, size = self._copy_bytes(source_file, target_dir / source_file.name)
            entries.append(
                SnapshotFileEntry(relative_path=source_file.name, size_bytes=size, sha256=digest)
            )
            path_hashes[source_file.name] = _hash_text(_canonical_path(source_file))
        payload = json.dumps({"source_path_hashes": path_hashes}, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        restrict_private_file(meta_path)
        entries.append(
            SnapshotFileEntry(
                relative_path=_SNAPSHOT_META_NAME,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    @staticmethod
    def _copy_bytes(source: Path, target: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
        # A snapshot copy is private runtime data whatever the source's mode
        # was (specification, 3.6).
        restrict_private_file(target)
        return digest.hexdigest(), size

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        path_hashes = self._read_snapshot_meta(snapshot_dir)
        scans = [self._scan_file(path) for path in self._transcript_files(snapshot_dir)]
        deduped = _dedup_candidates(scans)
        for scan, candidate in deduped:
            session_hash = _hash_text(scan.session_id)
            basis = (
                "explicit_user_role+origin_human"
                if candidate.origin_human
                else "explicit_user_role"
            )
            yield NormalizedUtterance(
                utterance_id=f"{ADAPTER_ID}-{session_hash[:16]}-{candidate.record_uuid}",
                source_adapter=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                session_hash=session_hash,
                timestamp=candidate.timestamp,
                text=candidate.text,
                modality=Modality.WRITTEN,
                text_status=TextStatus.VERBATIM,
                authorship_confidence=0.95 if candidate.origin_human else 0.9,
                authorship_basis=basis,
                source_path_hash=path_hashes.get(scan.file_name, instance.path_hash),
                content_flags=list(candidate.content_flags),
            )
        self._extraction_stats[instance.instance_key] = ExtractionStats(
            utterance_count=len(deduped),
            files_scanned=len(scans),
            unsupported_files=tuple(scan.file_name for scan in scans if scan.unsupported),
            truncated_tail_files=tuple(scan.file_name for scan in scans if scan.truncated_tail),
            malformed_lines=sum(scan.malformed_lines for scan in scans),
            structural_warnings=sum(scan.structural_warnings for scan in scans),
        )

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
        hashes = payload.get("source_path_hashes")
        if not isinstance(hashes, dict):
            return {}
        return {
            key: value
            for key, value in hashes.items()
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
        upper_bound = datetime.now(UTC) + timedelta(days=1)
        for utterance in utterances:
            if utterance.utterance_id in seen_ids:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "duplicate utterance ID after fork deduplication",
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
                        "utterance does not belong to the claude_code adapter",
                        item_ref=utterance.utterance_id,
                    )
                )
            if not utterance.text.strip():
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text is empty",
                        item_ref=utterance.utterance_id,
                    )
                )
            if any(marker in utterance.text for marker in _WRAPPER_MARKERS):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text contains an injected wrapper tag",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.timestamp is not None and not (
                _EARLIEST_PLAUSIBLE <= utterance.timestamp <= upper_bound
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance timestamp is outside the plausible range",
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
            if path.name in DENY_FILE_NAMES or any(part in DENY_DIR_NAMES for part in path.parts):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        "a denylisted path appears in the opened-path audit log",
                    )
                )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory registered by the adapter coordinator."""
    return ClaudeCodeAdapter()
