"""The OpenCode source adapter (``specifications/sources/opencode.md``).

Resolves the data root through ``XDG_DATA_HOME`` with the
``~/.local/share/opencode`` default, inventories every storage generation
present (WAL SQLite databases, the flat J2 JSON tree, and the per-project J1
JSON tree), and deduplicates records migrated between them by preserved
record ID. One source instance per (storage root, projectID) pair.

Databases are opened read-only (``mode=ro`` URI) and only the ``session``,
``message``, and ``part`` tables are ever queried; snapshots go through the
sqlite3 backup API and are sanitized (credential-bearing tables dropped,
``VACUUM``) before a marker allows extraction. ``auth.json``,
``mcp-auth.json``, config files, and every other denylisted path are never
opened; every open is audited against the allowlist first.
"""

import contextlib
import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.opencode.stores import (
    GENERATION_ORDER,
    VENDOR_TEMPLATE_TEXTS,
    Candidate,
    StoreScan,
    build_candidates,
    canonical_record_counts,
    canonical_versions,
    canonicalize,
    list_snapshot_source_files,
    project_keys,
    sanitize_snapshot_database,
    scan_j1_store,
    scan_j2_store,
    scan_sqlite_store,
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
    safe_id_part,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery.base import (
    DiscoveryContext,
    DiscoveryOutcome,
    SnapshotCapture,
    SourceAdapter,
)
from glite_english_audit.normalization.tokenizer import count_words

ADAPTER_ID = "opencode"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "OpenCode"
_STORAGE_FORMAT = "sqlite+json"
_SNAPSHOT_META_NAME = "opencode-snapshot-meta.json"
_SANITIZE_MARKER_SUFFIX = ".sanitized.json"

# Spec 2.3: files that must never be opened. Enumeration is allowlist-driven;
# these names are re-checked before every open as defense in depth.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {"auth.json", "mcp-auth.json", "opencode.json", "opencode.jsonc", "app.json"}
)
DENY_DIR_NAMES: frozenset[str] = frozenset(
    {
        "session_share",
        "todo",
        "permission",
        "session_diff",
        "snapshot",
        "log",
        "repos",
        "broken",
    }
)

_ALLOWED_JSON_PREFIXES = ("ses_", "msg_", "prt_")
_CHUNK_BYTES = 1 << 20
_MAX_TIMESTAMP = datetime.max.replace(tzinfo=UTC)
# OpenCode first shipped in 2025; anything earlier cannot be a real session.
_TIMESTAMP_FLOOR = datetime(2025, 1, 1, tzinfo=UTC)
_TIMESTAMP_FUTURE_SLACK = timedelta(days=2)
_POSIX_MODE_PRIVATE = stat.S_IRUSR | stat.S_IWUSR


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _instance_key(root: Path, project_key: str) -> str:
    """Stable per-(root, projectID) key; also serves as the path hash."""
    return _hash_text(f"{_canonical_path(root)}\x00{project_key}")


def _version_range(versions: set[str]) -> str | None:
    if not versions:
        return None
    ordered = sorted(versions)
    if len(ordered) == 1:
        return ordered[0]
    return f"{ordered[0]}-{ordered[-1]}"


def _storage_project_adjacency(path: Path) -> bool:
    parts = path.parts
    return any(
        parts[index] == "storage" and parts[index + 1] == "project"
        for index in range(len(parts) - 1)
    )


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    stores_scanned: int
    unsupported_stores: tuple[str, ...]
    malformed_records: int
    unrecognized_records: int
    mismatch_count: int


@dataclass(frozen=True)
class _Provisional:
    """One record's aggregates before opaque-label assignment."""

    source_path: Path
    key: str
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


def _degenerate(
    source_path: Path,
    key: str,
    *,
    accessibility: Accessibility,
    diagnostic_code: str | None,
    fingerprint: str,
) -> _Provisional:
    return _Provisional(
        source_path=source_path,
        key=key,
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


class OpenCodeAdapter:
    """SourceAdapter implementation for the OpenCode data store."""

    def __init__(self, wsl_mount_base: Path | None = None) -> None:
        # Fixed DrvFS mount root for Windows-host stores seen from WSL
        # (spec 2.4); injectable so tests never touch the real machine.
        self._wsl_mount_base = wsl_mount_base if wsl_mount_base is not None else Path("/mnt")
        self._override_db: Path | None = None
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

    # -- access guards -----------------------------------------------------

    def _assert_allowlisted(self, path: Path) -> None:
        name = path.name
        if (
            name in DENY_FILE_NAMES
            or any(part in DENY_DIR_NAMES for part in path.parts)
            or _storage_project_adjacency(path)
        ):
            msg = "refusing to open a path outside the OpenCode allowlist"
            raise PermissionError(msg)
        if path.suffix == ".db":
            return
        if name == "migration":
            return
        if name == _SNAPSHOT_META_NAME or name.endswith(_SANITIZE_MARKER_SUFFIX):
            return
        if name.endswith(".json") and name.startswith(_ALLOWED_JSON_PREFIXES):
            return
        msg = "refusing to open a path outside the OpenCode allowlist"
        raise PermissionError(msg)

    def _read_text(self, path: Path) -> str:
        self._assert_allowlisted(path)
        self._opened_paths.append(path)
        return path.read_text(encoding="utf-8", errors="replace")

    def _connect_readonly(self, path: Path) -> sqlite3.Connection:
        self._assert_allowlisted(path)
        self._opened_paths.append(path)
        # mode=ro, never immutable=1: the source may be a live WAL database.
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0)

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        root = self._data_root(context)
        self._override_db = self._resolve_override_db(root, context.environ)
        provisional: list[_Provisional] = []
        if context.os_environment is OsEnvironment.WSL:
            provisional.extend(self._wsl_host_hints())
        if not root.is_dir():
            provisional.append(
                _degenerate(
                    root,
                    _hash_text(_canonical_path(root)),
                    accessibility=Accessibility.NOT_FOUND,
                    diagnostic_code="SOURCE_NOT_FOUND",
                    fingerprint="absent",
                )
            )
            return self._label_and_build(context, provisional)
        scans = self._scan_root(root, self._database_paths(root))
        provisional.extend(self._store_problem_records(root, scans))
        instances = self._project_instances(root, scans)
        provisional.extend(instances)
        if not instances and not any(not scan.supported for scan in scans):
            fingerprint = self._root_fingerprint(scans)
            provisional.append(
                _degenerate(
                    root,
                    _hash_text(_canonical_path(root)),
                    accessibility=Accessibility.FOUND,
                    diagnostic_code=None,
                    fingerprint=fingerprint,
                )
            )
        return self._label_and_build(context, provisional)

    def _data_root(self, context: DiscoveryContext) -> Path:
        override = context.environ.get("XDG_DATA_HOME", "").strip()
        if override:
            return Path(override) / "opencode"
        return context.home / ".local" / "share" / "opencode"

    def _resolve_override_db(self, root: Path, environ: dict[str, str]) -> Path | None:
        value = environ.get("OPENCODE_DB", "").strip()
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / value
        return candidate if candidate.is_file() else None

    def _database_paths(self, root: Path) -> list[Path]:
        found = [
            entry
            for entry in sorted(root.glob("opencode*.db"))
            if entry.is_file() and not entry.is_symlink()
        ]
        if self._override_db is not None:
            seen = {_canonical_path(entry) for entry in found}
            if _canonical_path(self._override_db) not in seen:
                found.append(self._override_db)
        return found

    def _scan_root(self, root: Path, database_paths: list[Path]) -> list[StoreScan]:
        scans: list[StoreScan] = []
        order = 0
        for database in database_paths:
            scans.append(scan_sqlite_store(database, order, self._connect_readonly))
            order += 1
        j2 = scan_j2_store(root, order, self._read_text)
        if j2 is not None:
            scans.append(j2)
            order += 1
        j1 = scan_j1_store(root, order, self._read_text)
        if j1 is not None:
            scans.append(j1)
        return scans

    def _root_fingerprint(self, scans: list[StoreScan]) -> str:
        labels = [
            label
            for label in GENERATION_ORDER
            if any(scan.generation == label and scan.supported for scan in scans)
        ]
        return "+".join(labels) if labels else "empty"

    def _store_problem_records(self, root: Path, scans: list[StoreScan]) -> list[_Provisional]:
        records: list[_Provisional] = []
        for scan in scans:
            if scan.supported:
                continue
            accessibility = (
                Accessibility.UNSUPPORTED_SCHEMA
                if scan.diagnostic_code == "SOURCE_UNSUPPORTED_SCHEMA"
                else Accessibility.INACCESSIBLE
            )
            records.append(
                _degenerate(
                    root,
                    _hash_text(_canonical_path(scan.store_path)),
                    accessibility=accessibility,
                    diagnostic_code=scan.diagnostic_code,
                    fingerprint=scan.generation,
                )
            )
        return records

    def _project_instances(self, root: Path, scans: list[StoreScan]) -> list[_Provisional]:
        sessions, messages = canonicalize(scans)
        candidates = build_candidates(sessions, messages)
        by_project: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            by_project.setdefault(candidate.project_key, []).append(candidate)
        record_counts = canonical_record_counts(sessions, messages)
        versions = canonical_versions(sessions)
        fingerprint = self._root_fingerprint(scans)
        instances: list[_Provisional] = []
        for project in sorted(project_keys(sessions, messages)):
            project_candidates = by_project.get(project, [])
            stamps = [c.timestamp for c in project_candidates if c.timestamp is not None]
            instances.append(
                _Provisional(
                    source_path=root,
                    key=_instance_key(root, project),
                    accessibility=Accessibility.FOUND,
                    diagnostic_code=None,
                    schema_fingerprint=fingerprint,
                    app_version=_version_range(versions.get(project, set())),
                    estimated_records=record_counts.get(project, 0),
                    earliest=min(stamps) if stamps else None,
                    latest=max(stamps) if stamps else None,
                    messages=len(project_candidates),
                    words=sum(count_words(c.text) for c in project_candidates),
                    bytes_count=sum(len(c.text.encode("utf-8")) for c in project_candidates),
                )
            )
        return instances

    def _wsl_host_hints(self) -> list[_Provisional]:
        """Windows-host stores visible through DrvFS: hinted, never read."""
        hints: list[_Provisional] = []
        base = self._wsl_mount_base
        if not base.is_dir():
            return hints
        try:
            drives = sorted(base.iterdir())
        except OSError:
            return hints
        for drive in drives:
            if len(drive.name) != 1 or not drive.name.isalpha():
                continue
            users = drive / "Users"
            try:
                profiles = sorted(users.iterdir()) if users.is_dir() else []
            except OSError:
                continue
            for profile in profiles:
                # Fixed-layout existence check only; nothing is opened.
                host_root = profile / ".local" / "share" / "opencode"
                if host_root.is_dir():
                    hints.append(
                        _degenerate(
                            host_root,
                            _hash_text(_canonical_path(host_root)),
                            accessibility=Accessibility.INACCESSIBLE,
                            diagnostic_code="SOURCE_WSL_HOST_STORE_HINT",
                            fingerprint="wsl-host",
                        )
                    )
        return hints

    def _label_and_build(
        self, context: DiscoveryContext, provisional: list[_Provisional]
    ) -> DiscoveryOutcome:
        ordered = sorted(provisional, key=lambda item: (item.earliest or _MAX_TIMESTAMP, item.key))
        records: list[SourceInstanceRecord] = []
        instance_paths: dict[str, Path] = {}
        for position, item in enumerate(ordered, start=1):
            records.append(
                SourceInstanceRecord(
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    instance_key=item.key,
                    opaque_label=f"{_HUMAN_NAME} {position}",
                    storage_format=_STORAGE_FORMAT,
                    schema_fingerprint=item.schema_fingerprint,
                    path_hash=item.key,
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
            instance_paths[item.key] = item.source_path
        return DiscoveryOutcome(records=records, instance_paths=instance_paths)

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        if instance.diagnostic_code == "SOURCE_WSL_HOST_STORE_HINT":
            msg = "a Windows-host store seen from WSL is never snapshotted"
            raise PermissionError(msg)
        root = source_path
        ensure_private_dir(target_dir)
        entries: list[SnapshotFileEntry] = []
        skipped: list[str] = []
        for relative in list_snapshot_source_files(root):
            copied = self._copy_file_with_retry(root / relative, target_dir / relative)
            if copied is None:
                skipped.append(relative.as_posix())
                continue
            digest, size = copied
            entries.append(
                SnapshotFileEntry(relative_path=relative.as_posix(), size_bytes=size, sha256=digest)
            )
        used_names: set[str] = set()
        for database in self._database_paths(root):
            name = database.name
            if name in used_names:
                name = f"override-{name}"
            used_names.add(name)
            entries.extend(self._snapshot_database(database, target_dir, name))
        meta_payload = json.dumps(
            {
                "instance_key": instance.instance_key,
                "project_key": self._project_key_for_instance(root, instance.instance_key),
                "concurrent_write_skipped": skipped,
            },
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        entries.append(self._write_private_file(target_dir / _SNAPSHOT_META_NAME, meta_payload))
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    def _project_key_for_instance(self, root: Path, instance_key: str) -> str | None:
        scans = self._scan_root(root, self._database_paths(root))
        sessions, messages = canonicalize(scans)
        for project in sorted(project_keys(sessions, messages)):
            if _instance_key(root, project) == instance_key:
                return project
        return None

    def _copy_file_with_retry(self, source: Path, target: Path) -> tuple[str, int] | None:
        """Byte copy; one retry when the source changes size mid-copy (spec 8)."""
        for _ in range(2):
            try:
                size_before = source.stat().st_size
                digest, size = self._copy_bytes(source, target)
                if size == size_before and source.stat().st_size == size_before:
                    return digest, size
            except OSError:
                break
        target.unlink(missing_ok=True)
        return None

    def _copy_bytes(self, source: Path, target: Path) -> tuple[str, int]:
        self._assert_allowlisted(source)
        self._opened_paths.append(source)
        ensure_private_dir(target.parent)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
        self._chmod_private(target)
        return digest.hexdigest(), size

    def _snapshot_database(
        self, source_db: Path, target_dir: Path, name: str
    ) -> list[SnapshotFileEntry]:
        """Backup-API copy plus mandatory sanitize and marker (spec 8)."""
        target_db = target_dir / name
        source_connection = self._connect_readonly(source_db)
        try:
            target_connection = sqlite3.connect(target_db)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()
        self._chmod_private(target_db)
        try:
            dropped = sanitize_snapshot_database(target_db)
        except Exception:
            # An unsanitized backup transiently holds OAuth tokens: delete it
            # and fail the snapshot step rather than proceed (spec 9).
            target_db.unlink(missing_ok=True)
            raise
        digest = hashlib.sha256(target_db.read_bytes()).hexdigest()
        entries = [
            SnapshotFileEntry(
                relative_path=name, size_bytes=target_db.stat().st_size, sha256=digest
            )
        ]
        marker_payload = json.dumps(
            {"sanitized": True, "dropped_objects": dropped}, sort_keys=True
        ).encode("utf-8")
        entries.append(
            self._write_private_file(
                target_dir / f"{name}{_SANITIZE_MARKER_SUFFIX}", marker_payload
            )
        )
        return entries

    def _write_private_file(self, path: Path, payload: bytes) -> SnapshotFileEntry:
        ensure_private_dir(path.parent)
        path.write_bytes(payload)
        self._chmod_private(path)
        return SnapshotFileEntry(
            relative_path=path.name,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @staticmethod
    def _chmod_private(path: Path) -> None:
        with contextlib.suppress(OSError):
            path.chmod(_POSIX_MODE_PRIVATE)

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        project_key = self._read_snapshot_project_key(snapshot_dir)
        databases = sorted(entry for entry in snapshot_dir.glob("*.db") if entry.is_file())
        for database in databases:
            marker = snapshot_dir / f"{database.name}{_SANITIZE_MARKER_SUFFIX}"
            if not marker.is_file():
                msg = "refusing to extract from an unsanitized OpenCode snapshot"
                raise PermissionError(msg)
        if project_key is None:
            self._extraction_stats[instance.instance_key] = ExtractionStats(
                utterance_count=0,
                stores_scanned=0,
                unsupported_stores=(),
                malformed_records=0,
                unrecognized_records=0,
                mismatch_count=0,
            )
            return
        scans = self._scan_root(snapshot_dir, databases)
        sessions, messages = canonicalize(scans)
        candidates = build_candidates(sessions, messages)
        emitted = 0
        mismatches = 0
        for candidate in candidates:
            if candidate.project_key != project_key:
                continue
            emitted += 1
            if candidate.generation_text_mismatch:
                mismatches += 1
            yield self._utterance(instance, candidate)
        self._extraction_stats[instance.instance_key] = ExtractionStats(
            utterance_count=emitted,
            stores_scanned=len(scans),
            unsupported_stores=tuple(scan.generation for scan in scans if not scan.supported),
            malformed_records=sum(scan.malformed for scan in scans),
            unrecognized_records=sum(scan.unrecognized for scan in scans),
            mismatch_count=mismatches,
        )

    def _utterance(
        self, instance: SourceInstanceRecord, candidate: Candidate
    ) -> NormalizedUtterance:
        session_hash = _hash_text(candidate.session_id)
        flags: list[str] = []
        if candidate.generation_text_mismatch:
            flags.append("generation_text_mismatch")
        if candidate.timestamp is None:
            flags.append("undated")
        text_hash = _hash_text(candidate.text)
        # Spec 5.3: deterministic over adapter, session, message, part IDs,
        # and text hash.
        suffix = _hash_text(
            "\x00".join(
                [
                    ADAPTER_ID,
                    candidate.session_id,
                    candidate.message_id,
                    *candidate.part_ids,
                    text_hash,
                ]
            )
        )[:12]
        return NormalizedUtterance(
            utterance_id=(
                f"{ADAPTER_ID}-{session_hash[:16]}-{safe_id_part(candidate.message_id)}-{suffix}"
            ),
            source_adapter=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            session_hash=session_hash,
            timestamp=candidate.timestamp,
            text=candidate.text,
            modality=Modality.WRITTEN,
            text_status=TextStatus.VERBATIM,
            authorship_confidence=0.9,
            authorship_basis="user_role_text_part_non_synthetic",
            source_path_hash=instance.path_hash,
            destination_app=None,
            content_flags=flags,
        )

    def _read_snapshot_project_key(self, snapshot_dir: Path) -> str | None:
        meta_path = snapshot_dir / _SNAPSHOT_META_NAME
        if not meta_path.is_file():
            return None
        try:
            payload = json.loads(self._read_text(meta_path))
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        project_key = payload.get("project_key")
        return project_key if isinstance(project_key, str) and project_key else None

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
        ceiling = datetime.now(UTC) + _TIMESTAMP_FUTURE_SLACK
        for utterance in utterances:
            if utterance.utterance_id in seen_ids:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "duplicate utterance ID after cross-generation dedup",
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
                        "utterance does not belong to the opencode adapter",
                        item_ref=utterance.utterance_id,
                    )
                )
            if not utterance.text.strip():
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text is empty after trimming",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.text in VENDOR_TEMPLATE_TEXTS:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text matches a vendor command-template denylist entry",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.timestamp is not None and not (
                _TIMESTAMP_FLOOR <= utterance.timestamp <= ceiling
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance timestamp is outside the plausible range",
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
                    f"{instance.candidate_messages} candidate messages",
                    item_ref=instance.instance_key,
                )
            )
        for path in self._opened_paths:
            if (
                path.name in DENY_FILE_NAMES
                or any(part in DENY_DIR_NAMES for part in path.parts)
                or _storage_project_adjacency(path)
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        "a denylisted path appears in the opened-path audit log",
                    )
                )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory registered by the adapter coordinator."""
    return OpenCodeAdapter()
