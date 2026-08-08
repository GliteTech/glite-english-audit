"""OpenAI Codex CLI source adapter (``specifications/sources/codex.md``).

Root resolution goes through ``CODEX_HOME`` with the ``~/.codex`` default;
enumeration is allowlist-only (``sessions/YYYY/MM/DD/rollout-*.jsonl`` and
``archived_sessions/rollout-*.jsonl``), so ``auth.json``, ``config.toml``,
``history.jsonl``, and every other file under the root are never opened.
Snapshots are plain byte-for-byte copies written owner-only; extraction runs
against the snapshot alone and yields verbatim written user text.
"""

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.codex.rollout import (
    ROLLOUT_FILE_NAME,
    FileScan,
    FileStatus,
    scan_rollout_file,
)
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.io import ensure_private_dir
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
from glite_english_audit.normalization.tokenizer import count_words

ADAPTER_ID = "codex"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Codex"
_STORAGE_FORMAT = "jsonl"

# Spec 2.3 denylist. Enumeration is allowlist-driven, so this set is a test
# and review anchor, not a runtime gate.
NEVER_OPEN_NAMES = frozenset({"auth.json", "config.toml", "history.jsonl"})

# Fixed DrvFS mount root for Windows-host stores visible from WSL (spec 2.4).
# The one deliberate absolute path in this adapter; tests repoint it.
_WSL_MOUNT_BASE = Path("/mnt")

_SNAPSHOT_CHUNK_BYTES = 1 << 20
# Codex CLI first shipped in 2025; anything earlier cannot be a real session.
_TIMESTAMP_FLOOR = datetime(2025, 1, 1, tzinfo=UTC)
_TIMESTAMP_FUTURE_SLACK = timedelta(days=2)
_SORT_SENTINEL = datetime.max.replace(tzinfo=UTC)
_POSIX = os.name == "posix"


def _hash_path(path: Path) -> str:
    """SHA-256 over the canonical absolute path string."""
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _subdirs(parent: Path, digits: int) -> list[Path]:
    return sorted(
        child
        for child in parent.iterdir()
        if child.is_dir() and len(child.name) == digits and child.name.isdigit()
    )


def _rollout_files(directory: Path, root: Path) -> list[Path]:
    return [
        child.relative_to(root)
        for child in sorted(directory.iterdir())
        if child.is_file() and ROLLOUT_FILE_NAME.fullmatch(child.name)
    ]


def _iter_session_files(root: Path) -> list[Path]:
    """Every allowlisted rollout file under ``root``, as sorted relative paths.

    Anything else under the root — including the spec 2.3 denylist — is never
    opened and never reported.
    """
    found: list[Path] = []
    sessions = root / "sessions"
    if sessions.is_dir():
        for year in _subdirs(sessions, 4):
            for month in _subdirs(year, 2):
                for day in _subdirs(month, 2):
                    found.extend(_rollout_files(day, root))
    archived = root / "archived_sessions"
    if archived.is_dir():
        found.extend(_rollout_files(archived, root))
    return sorted(found)


def _windows_host_roots() -> list[Path]:
    """Windows-host Codex roots visible from WSL through DrvFS (spec 2.4)."""
    base = _WSL_MOUNT_BASE
    if not base.is_dir():
        return []
    try:
        drives = sorted(base.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for drive in drives:
        if len(drive.name) != 1 or not drive.name.isalpha():
            continue
        users = drive / "Users"
        try:
            profiles = sorted(users.iterdir()) if users.is_dir() else []
        except OSError:
            continue
        for profile in profiles:
            root = profile / ".codex"
            # Fixed-layout check only; profile directories are never walked.
            if (root / "sessions").is_dir():
                found.append(root)
    return found


def _candidate_roots(context: DiscoveryContext) -> list[Path]:
    """Spec 2.1/2.4 root resolution: ``CODEX_HOME``, else ``~/.codex``."""
    primary = context.home / ".codex"
    override = context.environ.get("CODEX_HOME", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            primary = candidate
    roots = [primary]
    if context.os_environment is OsEnvironment.WSL:
        roots.extend(_windows_host_roots())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


@dataclass(frozen=True)
class _InstanceProbe:
    """Aggregate discovery result for one root, before labeling."""

    root: Path
    path_hash: str
    accessibility: Accessibility
    diagnostic_code: str | None
    schema_fingerprint: str
    app_version: str | None
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    candidate_messages: int
    candidate_words: int
    candidate_bytes: int


def _empty_probe(
    root: Path, accessibility: Accessibility, diagnostic_code: str | None
) -> _InstanceProbe:
    return _InstanceProbe(
        root=root,
        path_hash=_hash_path(root),
        accessibility=accessibility,
        diagnostic_code=diagnostic_code,
        schema_fingerprint="none",
        app_version=None,
        estimated_records=0,
        earliest=None,
        latest=None,
        candidate_messages=0,
        candidate_words=0,
        candidate_bytes=0,
    )


def _probe_root(root: Path) -> _InstanceProbe:
    if not root.is_dir():
        return _empty_probe(root, Accessibility.NOT_FOUND, "SOURCE_NOT_FOUND")
    try:
        relative_files = _iter_session_files(root)
        scans: list[FileScan] = []
        for relative in relative_files:
            try:
                scans.append(scan_rollout_file(root / relative, relative.as_posix()))
            except OSError:
                # One unreadable file is skipped, never guessed about.
                continue
    except PermissionError:
        return _empty_probe(root, Accessibility.INACCESSIBLE, "SOURCE_INACCESSIBLE")

    supported = [scan for scan in scans if scan.status is FileStatus.SUPPORTED]
    unsupported_count = sum(1 for scan in scans if scan.status is FileStatus.UNSUPPORTED)
    accessibility = Accessibility.FOUND
    diagnostic_code: str | None = None
    if unsupported_count > 0 and not supported:
        # Spec 9: every parseable file is unsupported.
        accessibility = Accessibility.UNSUPPORTED_SCHEMA
        diagnostic_code = "SOURCE_UNSUPPORTED_SCHEMA"

    eligible = [scan for scan in supported if scan.eligible]
    candidates = [candidate for scan in eligible for candidate in scan.candidates]
    timestamps = sorted(
        candidate.timestamp for candidate in candidates if candidate.timestamp is not None
    )
    versions = sorted({version for scan in scans for version in scan.cli_versions})
    labels = sorted({scan.generation for scan in scans})
    return _InstanceProbe(
        root=root,
        path_hash=_hash_path(root),
        accessibility=accessibility,
        diagnostic_code=diagnostic_code,
        schema_fingerprint="+".join(labels) if labels else "empty",
        app_version=", ".join(versions) if versions else None,
        estimated_records=sum(scan.pre_filter_count for scan in supported),
        earliest=timestamps[0] if timestamps else None,
        latest=timestamps[-1] if timestamps else None,
        candidate_messages=len(candidates),
        candidate_words=sum(count_words(candidate.text) for candidate in candidates),
        candidate_bytes=sum(len(candidate.text.encode("utf-8")) for candidate in candidates),
    )


class CodexAdapter:
    """Source adapter for OpenAI Codex CLI rollout session stores."""

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        return Stability.STABLE

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        probes = [_probe_root(root) for root in _candidate_roots(context)]
        probes.sort(key=lambda probe: (probe.earliest or _SORT_SENTINEL, probe.path_hash))
        records: list[SourceInstanceRecord] = []
        instance_paths: dict[str, Path] = {}
        for position, probe in enumerate(probes, start=1):
            records.append(
                SourceInstanceRecord(
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    instance_key=probe.path_hash,
                    opaque_label=f"{_HUMAN_NAME} {position}",
                    storage_format=_STORAGE_FORMAT,
                    schema_fingerprint=probe.schema_fingerprint,
                    path_hash=probe.path_hash,
                    os_environment=context.os_environment,
                    app_version=probe.app_version,
                    stability=Stability.STABLE,
                    accessibility=probe.accessibility,
                    diagnostic_code=probe.diagnostic_code,
                    estimated_records=probe.estimated_records,
                    earliest_timestamp=probe.earliest,
                    latest_timestamp=probe.latest,
                    candidate_messages=probe.candidate_messages,
                    candidate_words=probe.candidate_words,
                    candidate_bytes=probe.candidate_bytes,
                )
            )
            instance_paths[probe.path_hash] = probe.root
        return DiscoveryOutcome(records=records, instance_paths=instance_paths)

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        entries: list[SnapshotFileEntry] = []
        for relative in _iter_session_files(source_path):
            source_file = source_path / relative
            target_file = target_dir / relative
            ensure_private_dir(target_file.parent)
            digest = hashlib.sha256()
            size = 0
            with source_file.open("rb") as reader, target_file.open("wb") as writer:
                while chunk := reader.read(_SNAPSHOT_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    writer.write(chunk)
            if _POSIX:
                # Spec 8: snapshot copies are 0600 regardless of source modes.
                target_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            entries.append(
                SnapshotFileEntry(
                    relative_path=relative.as_posix(),
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                )
            )
        return SnapshotCapture(
            snapshot_relative_dir=f"{ADAPTER_ID}/{instance.instance_key[:16]}",
            files=entries,
        )

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        for relative in _iter_session_files(snapshot_dir):
            scan = scan_rollout_file(snapshot_dir / relative, relative.as_posix())
            if scan.status is not FileStatus.SUPPORTED or not scan.eligible:
                continue
            if scan.session_id is None:
                continue
            session_hash = _hash_session_id(scan.session_id)
            file_flags: list[str] = []
            if scan.truncated_tail:
                file_flags.append("truncated_tail")
            if scan.channel_mismatch:
                file_flags.append("channel_mismatch")
            if scan.forked:
                file_flags.append("forked_from")
            if scan.session_id_mismatch:
                file_flags.append("session_id_mismatch")
            for candidate in scan.candidates:
                text_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
                utterance_id = (
                    f"{ADAPTER_ID}-{session_hash[:16]}-L{candidate.line_index:06d}-{text_hash[:12]}"
                )
                yield NormalizedUtterance(
                    utterance_id=utterance_id,
                    source_adapter=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    session_hash=session_hash,
                    timestamp=candidate.timestamp,
                    text=candidate.text,
                    modality=Modality.WRITTEN,
                    text_status=TextStatus.VERBATIM,
                    authorship_confidence=candidate.confidence,
                    authorship_basis=candidate.basis,
                    source_path_hash=instance.path_hash,
                    destination_app=None,
                    content_flags=list(file_flags),
                )

    def verify(
        self,
        instance: SourceInstanceRecord,
        utterances: list[NormalizedUtterance],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        seen: dict[str, int] = {}
        for utterance in utterances:
            seen[utterance.utterance_id] = seen.get(utterance.utterance_id, 0) + 1
        for utterance_id, count in sorted(seen.items()):
            if count > 1:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        f"utterance ID emitted {count} times",
                        item_ref=utterance_id,
                    )
                )
        ceiling = datetime.now(UTC) + _TIMESTAMP_FUTURE_SLACK
        for utterance in utterances:
            if not utterance.text.strip():
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text is empty after trimming",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.source_adapter != ADAPTER_ID:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        f"utterance carries foreign source_adapter {utterance.source_adapter!r}",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.timestamp is not None and (
                utterance.timestamp.tzinfo is None
                or utterance.timestamp < _TIMESTAMP_FLOOR
                or utterance.timestamp > ceiling
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance timestamp is naive or outside the plausible range",
                        item_ref=utterance.utterance_id,
                    )
                )
        if (
            instance.accessibility is Accessibility.FOUND
            and len(utterances) != instance.candidate_messages
        ):
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"extracted {len(utterances)} utterances but discovery counted "
                    f"{instance.candidate_messages}",
                    item_ref=instance.instance_key,
                )
            )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory used by the discovery registry."""
    return CodexAdapter()
