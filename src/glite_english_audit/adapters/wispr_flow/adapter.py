"""Wispr Flow dictation source adapter (``specifications/sources/wispr_flow.md``).

One SQLite store per machine: ``<data root>/flow.sqlite``, where the data
root is ``~/Library/Application Support/Wispr Flow`` on macOS and
``%APPDATA%\\Wispr Flow`` on Windows. The adapter opens exactly that file
(plus ``-wal``/``-shm`` sidecars during a fallback copy); every other file in
the data root — ``config.json``, Electron profile content, ``backup-*.sqlite``
generations, logs — is denylisted and never opened.

The schema fingerprint comes from secondary evidence only, so it gates
everything: any store whose ``History`` shape does not match fails closed as
``unsupported_schema`` with zero counts and zero text. Only the raw
``asrText`` column is ever extracted (``modality=spoken_asr``,
``text_status=verbatim``); formatted, edited, clipboard, accessibility,
audio, and context columns sit in the same table and stay on the
never-ingest list. The live database is copied per spec 4.6 (backup API from
a ``mode=ro`` connection, WAL-aware byte-copy fallback), never read directly
and never treated as a flat file.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from glite_english_audit.adapters.wispr_flow.store import (
    NEVER_INGEST_COLUMNS,
    StoreScan,
    inspect_structure,
    scan_history,
    structure_fingerprint,
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

ADAPTER_ID = "wispr_flow"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Wispr Flow"
_STORAGE_FORMAT = "sqlite"
_DB_NAME = "flow.sqlite"
_SNAPSHOT_META_NAME = "wispr-flow-snapshot-meta.json"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_DATA_DIR_NAME = "Wispr Flow"

# The only files the adapter may ever open at a source location (spec 2).
ALLOWED_SOURCE_NAMES: frozenset[str] = frozenset({_DB_NAME, f"{_DB_NAME}-wal", f"{_DB_NAME}-shm"})

# Spec 2 denylist anchors for the opened-path audit and tests. Enumeration
# never happens — the adapter goes straight to flow.sqlite — so these are
# defense in depth, not a runtime gate.
NEVER_OPEN_NAMES: frozenset[str] = frozenset(
    {
        "config.json",
        "SharedStorage",
        "SharedStorage-wal",
        "Preferences",
        "Cookies",
        "main.log",
        "accessibility.log",
        "wispr-flow.mcpb",
    }
)
NEVER_OPEN_DIR_NAMES: frozenset[str] = frozenset(
    {
        "Local Storage",
        "Session Storage",
        "IndexedDB",
        "Cache",
        "Code Cache",
        "GPUCache",
        "blob_storage",
        "Crashpad",
        "logs",
        "Logs",
    }
)
_BACKUP_DB_PREFIX = "backup-"

# Fixed DrvFS mount root for Windows-host stores visible from WSL (spec 1).
# The one deliberate absolute path in this adapter; tests repoint it.
_WSL_MOUNT_BASE = Path("/mnt")

_PK_ANOMALY_RATIO_LIMIT = 0.01
_BACKUP_ATTEMPTS = 3
_BACKUP_RETRY_SECONDS = 0.05
# Earliest known History migrations are 2024-05 (spec 3.4).
_TIMESTAMP_FLOOR = datetime(2024, 1, 1, tzinfo=UTC)
_TIMESTAMP_FUTURE_SLACK = timedelta(days=1)
_POSIX = os.name == "posix"
_COLUMN_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


class _CopyFailure(Exception):
    """No consistent copy of the live store could be produced (spec 6.2)."""

    def __init__(self, kind: Literal["locked", "corrupt"]) -> None:
        super().__init__(kind)
        self.kind: Literal["locked", "corrupt"] = kind


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    total_rows: int
    empty_asr_rows: int
    pk_anomalies: int
    unknown_status_rows: int
    wordcount_divergent_rows: int


_ZERO_STATS = ExtractionStats(
    utterance_count=0,
    total_rows=0,
    empty_asr_rows=0,
    pk_anomalies=0,
    unknown_status_rows=0,
    wordcount_divergent_rows=0,
)


@dataclass(frozen=True)
class _StoreProbe:
    """Aggregate discovery result for one store, before record assembly."""

    root: Path
    accessibility: Accessibility
    diagnostic_code: str | None
    fingerprint: str
    app_version: str | None = None
    estimated_records: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    candidate_messages: int = 0
    candidate_words: int = 0
    candidate_bytes: int = 0


class WisprFlowAdapter:
    """SourceAdapter implementation for the Wispr Flow dictation store."""

    def __init__(self) -> None:
        self._opened_paths: list[Path] = []
        self._executed_sql: list[str] = []
        self._extraction_stats: dict[str, ExtractionStats] = {}
        self._last_journal_mode: str = "unknown"

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        # Spec header: beta until the section 9 real-installation smoke tests
        # confirm the fingerprint on macOS and native Windows.
        return Stability.BETA

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        if context.os_environment is OsEnvironment.WSL:
            probes = self._discover_wsl()
        elif context.os_environment is OsEnvironment.LINUX:
            # Spec 1: no native Linux application; never probed.
            probes = [
                _StoreProbe(
                    root=context.home / _DATA_DIR_NAME,
                    accessibility=Accessibility.NOT_FOUND,
                    diagnostic_code="SOURCE_NOT_FOUND",
                    fingerprint="absent",
                )
            ]
        else:
            probes = [self._probe_native(self._data_root(context))]
        return self._build_outcome(context, probes)

    def _data_root(self, context: DiscoveryContext) -> Path:
        if context.os_environment is OsEnvironment.WINDOWS:
            appdata = context.environ.get("APPDATA", "").strip()
            roaming = Path(appdata) if appdata else context.home / "AppData" / "Roaming"
            return roaming / _DATA_DIR_NAME
        return context.home / "Library" / "Application Support" / _DATA_DIR_NAME

    def _probe_native(self, root: Path) -> _StoreProbe:
        db = root / _DB_NAME
        if not db.is_file() or db.is_symlink():
            return _StoreProbe(
                root=root,
                accessibility=Accessibility.NOT_FOUND,
                diagnostic_code="SOURCE_NOT_FOUND",
                fingerprint="absent",
            )
        return self._probe_store(root, db)

    def _discover_wsl(self) -> list[_StoreProbe]:
        """Spec 1: a Windows-host store seen from WSL fails closed, unopened."""
        probes: list[_StoreProbe] = []
        for root in self._wsl_host_roots():
            probes.append(
                _StoreProbe(
                    root=root,
                    accessibility=Accessibility.INACCESSIBLE,
                    diagnostic_code="SOURCE_WSL_HOST_STORE_HINT",
                    fingerprint="wsl-host-store",
                )
            )
        if not probes:
            probes.append(
                _StoreProbe(
                    root=_WSL_MOUNT_BASE,
                    accessibility=Accessibility.NOT_FOUND,
                    diagnostic_code="SOURCE_NOT_FOUND",
                    fingerprint="absent",
                )
            )
        return probes

    def _wsl_host_roots(self) -> list[Path]:
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
                root = profile / "AppData" / "Roaming" / _DATA_DIR_NAME
                # Fixed-layout existence check only; the file is never opened.
                if (root / _DB_NAME).is_file():
                    found.append(root)
        return found

    def _probe_store(self, root: Path, db: Path) -> _StoreProbe:
        try:
            if not self._has_sqlite_magic(db):
                return _StoreProbe(
                    root=root,
                    accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                    diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                    fingerprint="not-sqlite",
                )
        except PermissionError:
            # macOS TCC can block reads; surfaced as inaccessible, never
            # retried with elevation (spec 1).
            return _StoreProbe(
                root=root,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        except OSError:
            return _StoreProbe(
                root=root,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        # Discovery never queries the live database either: inspect a
        # consistent temporary copy, then discard it (spec 6.1 step 3).
        with tempfile.TemporaryDirectory(prefix="wispr-flow-discovery-") as temp_dir:
            copy = Path(temp_dir) / _DB_NAME
            try:
                self._consistent_copy(db, copy)
            except _CopyFailure as failure:
                if failure.kind == "locked":
                    return _StoreProbe(
                        root=root,
                        accessibility=Accessibility.INACCESSIBLE,
                        diagnostic_code="SOURCE_LOCKED",
                        fingerprint="locked",
                    )
                return _StoreProbe(
                    root=root,
                    accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                    diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                    fingerprint="corrupt",
                )
            return self._inspect_copy(root, copy)

    def _inspect_copy(self, root: Path, copy: Path) -> _StoreProbe:
        connection = self._connect_ro(copy)
        try:
            structure = inspect_structure(connection, self._executed_sql)
            if not structure.supported:
                return _StoreProbe(
                    root=root,
                    accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                    diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                    fingerprint=structure_fingerprint(structure),
                )
            scan = scan_history(connection, structure, self._executed_sql)
        except sqlite3.DatabaseError:
            return _StoreProbe(
                root=root,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="corrupt",
            )
        finally:
            connection.close()
        if scan.pk_anomaly_ratio > _PK_ANOMALY_RATIO_LIMIT:
            # Spec 8: systematic primary-key anomalies mean the shape does
            # not match the fingerprinted schema after all.
            return _StoreProbe(
                root=root,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="unsupported:pk-anomalies",
            )
        stamps = [row.timestamp for row in scan.rows if row.timestamp is not None]
        return _StoreProbe(
            root=root,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            fingerprint=structure_fingerprint(structure),
            app_version=", ".join(scan.app_versions) if scan.app_versions else None,
            estimated_records=scan.total_rows,
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            candidate_messages=len(scan.rows),
            candidate_words=sum(row.word_count for row in scan.rows),
            candidate_bytes=sum(len(row.text.encode("utf-8")) for row in scan.rows),
        )

    def _build_outcome(
        self, context: DiscoveryContext, probes: list[_StoreProbe]
    ) -> DiscoveryOutcome:
        ordered = sorted(
            ((probe, _hash_text(_canonical_path(probe.root))) for probe in probes),
            key=lambda item: (item[0].earliest or datetime.max.replace(tzinfo=UTC), item[1]),
        )
        records: list[SourceInstanceRecord] = []
        instance_paths: dict[str, Path] = {}
        for position, (probe, path_hash) in enumerate(ordered, start=1):
            records.append(
                SourceInstanceRecord(
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    instance_key=path_hash,
                    opaque_label=f"{_HUMAN_NAME} {position}",
                    storage_format=_STORAGE_FORMAT,
                    schema_fingerprint=probe.fingerprint,
                    path_hash=path_hash,
                    os_environment=context.os_environment,
                    app_version=probe.app_version,
                    stability=self.stability,
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
            instance_paths[path_hash] = probe.root
        return DiscoveryOutcome(records=records, instance_paths=instance_paths)

    # -- file access guards ------------------------------------------------

    def _assert_source_allowlisted(self, path: Path) -> None:
        if (
            path.name not in ALLOWED_SOURCE_NAMES
            or path.name.startswith(_BACKUP_DB_PREFIX)
            or any(part in NEVER_OPEN_DIR_NAMES for part in path.parts)
        ):
            msg = "refusing to open a path outside the flow.sqlite allowlist"
            raise PermissionError(msg)

    def _has_sqlite_magic(self, db: Path) -> bool:
        self._assert_source_allowlisted(db)
        self._opened_paths.append(db)
        with db.open("rb") as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC

    def _connect_ro(self, db: Path) -> sqlite3.Connection:
        self._assert_source_allowlisted(db)
        self._opened_paths.append(db)
        uri = f"{db.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=1.0)

    # -- consistent copying (spec 6.2) -------------------------------------

    def _backup(self, source_db: Path, target_db: Path) -> None:
        source = self._connect_ro(source_db)
        try:
            self._last_journal_mode = self._read_journal_mode(source)
            self._opened_paths.append(target_db)
            destination = sqlite3.connect(str(target_db))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    def _read_journal_mode(self, connection: sqlite3.Connection) -> str:
        pragma = "PRAGMA journal_mode"
        self._executed_sql.append(pragma)
        try:
            return str(connection.execute(pragma).fetchone()[0])
        except sqlite3.DatabaseError:
            return "unknown"

    def _integrity_ok(self, db: Path) -> bool:
        self._assert_source_allowlisted(db)
        self._opened_paths.append(db)
        pragma = "PRAGMA integrity_check"
        try:
            connection = sqlite3.connect(str(db))
            try:
                self._executed_sql.append(pragma)
                result = connection.execute(pragma).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False
        return bool(result) and str(result[0]).lower() == "ok"

    def _consistent_copy(self, source_db: Path, target_db: Path) -> list[Path]:
        """Backup-API-first copy; WAL-aware byte-copy fallback (spec 6.2)."""
        saw_lock = False
        for _ in range(_BACKUP_ATTEMPTS):
            try:
                self._backup(source_db, target_db)
            except sqlite3.OperationalError as error:
                message = str(error).lower()
                if "locked" in message or "busy" in message:
                    saw_lock = True
                    time.sleep(_BACKUP_RETRY_SECONDS)
                    continue
                break
            except sqlite3.DatabaseError:
                break
            if self._integrity_ok(target_db):
                return [target_db]
            # A clean backup that fails integrity means the source is corrupt.
            target_db.unlink(missing_ok=True)
            raise _CopyFailure("corrupt")
        for _ in range(2):
            copied = self._copy_store_files(source_db, target_db)
            if self._integrity_ok(target_db):
                return copied
            for path in copied:
                path.unlink(missing_ok=True)
        raise _CopyFailure("locked" if saw_lock else "corrupt")

    def _copy_store_files(self, source_db: Path, target_db: Path) -> list[Path]:
        """Byte-copy main+WAL+SHM together; never the main file alone."""
        copied: list[Path] = []
        for suffix in ("", "-wal", "-shm"):
            source = source_db.parent / f"{source_db.name}{suffix}"
            if not source.is_file():
                continue
            target = target_db.parent / f"{target_db.name}{suffix}"
            self._assert_source_allowlisted(source)
            self._opened_paths.append(source)
            with source.open("rb") as reader, target.open("wb") as writer:
                while chunk := reader.read(1 << 20):
                    writer.write(chunk)
            copied.append(target)
        return copied

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        ensure_private_dir(target_dir)
        source_db = source_path / _DB_NAME
        try:
            copied = self._consistent_copy(source_db, target_dir / _DB_NAME)
        except _CopyFailure as failure:
            msg = f"no consistent snapshot of {_DB_NAME} could be taken ({failure.kind})"
            raise RuntimeError(msg) from failure
        entries: list[SnapshotFileEntry] = []
        for path in sorted(copied):
            if not path.is_file():
                # The integrity check may have absorbed a copied WAL into the
                # main file on close; the manifest lists surviving files only.
                continue
            if _POSIX:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            digest, size = self._file_digest(path)
            entries.append(
                SnapshotFileEntry(relative_path=path.name, size_bytes=size, sha256=digest)
            )
        payload = json.dumps(
            {
                "source_path_hash": _hash_text(_canonical_path(source_db)),
                "journal_mode": self._last_journal_mode,
            },
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        if _POSIX:
            meta_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        entries.append(
            SnapshotFileEntry(
                relative_path=_SNAPSHOT_META_NAME,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        return SnapshotCapture(
            snapshot_relative_dir=f"{ADAPTER_ID}/{instance.instance_key[:16]}",
            files=entries,
        )

    @staticmethod
    def _file_digest(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as reader:
            while chunk := reader.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        self._extraction_stats[instance.instance_key] = _ZERO_STATS
        db = snapshot_dir / _DB_NAME
        if not db.is_file():
            return
        source_path_hash = self._read_snapshot_meta(snapshot_dir) or instance.path_hash
        try:
            connection = self._connect_ro(db)
        except sqlite3.Error:
            return
        try:
            structure = inspect_structure(connection, self._executed_sql)
            if not structure.supported:
                # Fingerprint gate: unknown shapes yield nothing, ever.
                return
            scan = scan_history(connection, structure, self._executed_sql)
        except sqlite3.DatabaseError:
            return
        finally:
            connection.close()
        if scan.pk_anomaly_ratio > _PK_ANOMALY_RATIO_LIMIT:
            return
        self._extraction_stats[instance.instance_key] = self._stats_from_scan(scan)
        for row in scan.rows:
            if row.conversation_id is not None:
                session_basis = f"{ADAPTER_ID}|conversation|{row.conversation_id}"
            else:
                session_basis = f"{ADAPTER_ID}|transcript|{row.transcript_entity_id}"
            session_hash = _hash_text(session_basis)
            yield NormalizedUtterance(
                utterance_id=f"{ADAPTER_ID}-{session_hash[:16]}-{row.transcript_entity_id}",
                source_adapter=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                session_hash=session_hash,
                timestamp=row.timestamp,
                text=row.text,
                modality=Modality.SPOKEN_ASR,
                text_status=TextStatus.VERBATIM,
                authorship_confidence=0.95,
                authorship_basis="sole_dictation_field",
                source_path_hash=source_path_hash,
                destination_app=row.destination_app,
                content_flags=list(row.content_flags),
            )

    @staticmethod
    def _stats_from_scan(scan: StoreScan) -> ExtractionStats:
        return ExtractionStats(
            utterance_count=len(scan.rows),
            total_rows=scan.total_rows,
            empty_asr_rows=scan.empty_asr_rows,
            pk_anomalies=scan.pk_anomalies,
            unknown_status_rows=scan.unknown_status_rows,
            wordcount_divergent_rows=scan.wordcount_divergent_rows,
        )

    def _read_snapshot_meta(self, snapshot_dir: Path) -> str | None:
        meta_path = snapshot_dir / _SNAPSHOT_META_NAME
        if not meta_path.is_file():
            return None
        self._opened_paths.append(meta_path)
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("source_path_hash")
        return value if isinstance(value, str) and value else None

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
                        "duplicate utterance ID within one instance",
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
                        "utterance does not belong to the wispr_flow adapter",
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
            elif (
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
                        "utterance timestamp lies outside the instance's reported range",
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
        diagnostics.extend(self._audit_opened_paths())
        diagnostics.extend(self._audit_executed_sql())
        return diagnostics

    def _audit_opened_paths(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for path in self._opened_paths:
            if (
                path.name in NEVER_OPEN_NAMES
                or path.name.startswith(_BACKUP_DB_PREFIX)
                or any(part in NEVER_OPEN_DIR_NAMES for part in path.parts)
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        "a denylisted path appears in the opened-path audit log",
                    )
                )
        return diagnostics

    def _audit_executed_sql(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for statement in self._executed_sql:
            tokens = {token.lower() for token in _COLUMN_TOKEN.findall(statement)}
            if tokens & NEVER_INGEST_COLUMNS:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "a never-ingest column name appears in the executed SQL audit log",
                    )
                )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory registered by the adapter coordinator."""
    return WisprFlowAdapter()
