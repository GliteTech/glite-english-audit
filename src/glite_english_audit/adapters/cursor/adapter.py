"""The Cursor IDE source adapter (beta, inventory only).

Implements ``specifications/sources/cursor.md`` for the verified macOS G4
global bubble store: discovery opens the live ``state.vscdb`` read-only for
aggregate counting, snapshotting uses the SQLite backup API, and extraction
scans the snapshot for count verification but yields no utterances. Cursor's
rawness is unknown (native Voice Mode, spec section 5 and 10.1), so under
project specification 4.7 the adapter inventories chat data — counts and date
ranges — and contributes no analyzable text while it remains beta.

The adapter opens only ``globalStorage/state.vscdb``,
``workspaceStorage/<hash>/state.vscdb``, and
``workspaceStorage/<hash>/workspace.json`` (spec section 3). File and key
accesses are audited; verify() fails on any denylisted access.
"""

import contextlib
import hashlib
import json
import shutil
import sqlite3
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from glite_english_audit.adapters.cursor.store import (
    ALLOWED_ITEM_PRESENCE_KEYS,
    ALLOWED_ITEM_VALUE_KEYS,
    ALLOWED_KV_PREFIXES,
    LEGACY_AICHAT_KEY,
    REQUIRED_TABLES,
    WORKSPACE_INDEX_KEY,
    GlobalStoreScan,
    StateDatabase,
    parse_workspace_index,
    scan_global_store,
)
from glite_english_audit.artifacts.enums import Accessibility, OsEnvironment, Stability
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

ADAPTER_ID = "cursor"
ADAPTER_VERSION = "0.1.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Cursor"
_STORAGE_FORMAT = "sqlite"
_SNAPSHOT_META_NAME = "cursor-snapshot-meta.json"
_GLOBAL_DB_RELATIVE = "globalStorage/state.vscdb"

# Spec 4.7 (project) and cursor spec sections 5/10.1: rawness is unknown, so
# the beta adapter records this reason instead of emitting any utterance.
BETA_NO_TEXT_REASON = "beta_rawness_unknown_no_analyzable_text"

# Names the adapter must never open (spec section 3). Enumeration is
# allowlist-only (exactly three file names), so this set is defense in depth
# for the opened-path audit, not the primary gate.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        "state.vscdb.backup",
        "storage.json",
        "mcp.json",
        "prompt_history.json",
        "ai-code-tracking.db",
        "store.db",
        "Preferences",
        "Cookies",
        "Network Persistent State",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".cursor",
        ".cursor-server",
        "History",
        "Backups",
        "Local Storage",
        "Session Storage",
        "WebStorage",
        "Crashpad",
        "sentry",
        "agent-transcripts",
        "ai-tracking",
        "anysphere.cursor-retrieval",
        "anysphere.cursor-commits",
        "anysphere.cursor-mcp",
    }
)
# The only file names discovery, snapshot, or extraction may ever open.
_ALLOWED_OPEN_NAMES: frozenset[str] = frozenset({"state.vscdb", "workspace.json"})

# Fixed DrvFS mount root for the WSL host-store hint (spec section 1).
# The one deliberate absolute path in this adapter; tests repoint it.
_WSL_MOUNT_BASE = Path("/mnt")

_BACKUP_ATTEMPTS = 3
_BACKUP_RETRY_SLEEP_SECONDS = 0.2
_FREE_SPACE_MARGIN_BYTES = 64 << 20
_HASH_CHUNK_BYTES = 1 << 20
_POSIX_MODE_FILE = stat.S_IRUSR | stat.S_IWUSR


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _hash_path(path: Path) -> str:
    return _hash_text(_canonical_path(path))


@dataclass(frozen=True)
class CursorInventoryStats:
    """Per-instance discovery accounting kept locally for verification."""

    scan: GlobalStoreScan | None
    workspaces_indexed: int
    workspace_composer_links: int
    workspace_folder_hashes: tuple[str, ...]
    legacy_g1_workspaces: int
    legacy_g2_workspaces: int
    g5_store_detected: bool
    g5_chat_sessions: int
    cursor_server_detected: bool


@dataclass(frozen=True)
class CursorExtractionStats:
    """Why extraction emitted nothing, plus the snapshot re-scan counts."""

    reason: str
    scan: GlobalStoreScan | None


@dataclass(frozen=True)
class _Probe:
    """One probed instance root before record assembly."""

    root: Path
    accessibility: Accessibility
    diagnostic_code: str | None
    fingerprint: str
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    candidate_messages: int
    candidate_words: int
    candidate_bytes: int
    stats: CursorInventoryStats


def _windows_host_store_seen() -> bool:
    """Fixed-layout probe for a Windows-host store visible through DrvFS."""
    base = _WSL_MOUNT_BASE
    if not base.is_dir():
        return False
    try:
        drives = sorted(base.iterdir())
    except OSError:
        return False
    for drive in drives:
        if len(drive.name) != 1 or not drive.name.isalpha():
            continue
        users = drive / "Users"
        try:
            profiles = sorted(users.iterdir()) if users.is_dir() else []
        except OSError:
            continue
        for profile in profiles:
            store = profile / "AppData" / "Roaming" / "Cursor" / "User" / _GLOBAL_DB_RELATIVE
            # Existence check only; the host database is never opened (spec 1).
            if store.is_file():
                return True
    return False


def _fingerprint(stats: CursorInventoryStats) -> str:
    scan = stats.scan
    generations: list[str] = []
    if stats.legacy_g1_workspaces:
        generations.append("g1")
    if stats.legacy_g2_workspaces:
        generations.append("g2")
    if scan is not None and scan.composers_g3:
        generations.append("g3")
    if scan is not None and scan.composers_g4:
        generations.append("g4")
    if stats.g5_store_detected:
        generations.append("g5")
    parts = ["+".join(generations) if generations else "empty"]
    if scan is not None and scan.composer_versions:
        low, high = min(scan.composer_versions), max(scan.composer_versions)
        parts.append(f"composer_v={low}" if low == high else f"composer_v={low}-{high}")
    if scan is not None and scan.bubble_versions:
        parts.append("bubble_v=" + ",".join(str(v) for v in sorted(scan.bubble_versions)))
    return ";".join(parts)


def _legacy_material_present(stats: CursorInventoryStats) -> bool:
    scan = stats.scan
    in_store = scan is not None and (
        scan.composers_g3 > 0
        or scan.composers_unsupported_version > 0
        or scan.composers_malformed > 0
        or scan.composers_id_mismatch > 0
    )
    return (
        in_store
        or stats.legacy_g1_workspaces > 0
        or stats.legacy_g2_workspaces > 0
        or stats.g5_store_detected
    )


class CursorAdapter:
    """SourceAdapter implementation for the Cursor IDE chat store (beta)."""

    def __init__(self) -> None:
        self._opened_paths: list[Path] = []
        self._key_audit: list[tuple[str, str]] = []
        self._inventory_stats: dict[str, CursorInventoryStats] = {}
        self._extraction_stats: dict[str, CursorExtractionStats] = {}

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        return Stability.BETA

    # -- access guards -----------------------------------------------------

    def _assert_allowed_file(self, path: Path) -> None:
        if path.name not in _ALLOWED_OPEN_NAMES or any(
            part in DENY_DIR_NAMES for part in path.parts
        ):
            msg = f"refusing to open a path outside the Cursor allowlist: {path.name}"
            raise PermissionError(msg)

    def _record_key_access(self, kind: str, key: str) -> None:
        self._key_audit.append((kind, key))

    def _open_state_db(self, path: Path) -> StateDatabase:
        self._assert_allowed_file(path)
        self._opened_paths.append(path)
        return StateDatabase.open_readonly(path, self._record_key_access)

    def opened_key_audit(self) -> tuple[tuple[str, str], ...]:
        """Every (kind, key) database access this adapter instance made."""
        return tuple(self._key_audit)

    def opened_paths(self) -> tuple[Path, ...]:
        """Every source file this adapter instance opened."""
        return tuple(self._opened_paths)

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        if context.os_environment is OsEnvironment.WSL:
            probe = self._wsl_probe(context)
        else:
            root = self._user_data_root(context)
            probe = self._probe_root(root, context.home)
        record = self._build_record(context, probe, position=1)
        self._inventory_stats[record.instance_key] = probe.stats
        return DiscoveryOutcome(records=[record], instance_paths={record.path_hash: probe.root})

    def _user_data_root(self, context: DiscoveryContext) -> Path:
        if context.os_environment is OsEnvironment.MACOS:
            return context.home / "Library" / "Application Support" / "Cursor" / "User"
        if context.os_environment is OsEnvironment.WINDOWS:
            appdata = context.environ.get("APPDATA", "").strip()
            base = Path(appdata) if appdata else context.home / "AppData" / "Roaming"
            return base / "Cursor" / "User"
        return context.home / ".config" / "Cursor" / "User"

    def _empty_stats(
        self, *, g5_detected: bool = False, g5_sessions: int = 0, server: bool = False
    ) -> CursorInventoryStats:
        return CursorInventoryStats(
            scan=None,
            workspaces_indexed=0,
            workspace_composer_links=0,
            workspace_folder_hashes=(),
            legacy_g1_workspaces=0,
            legacy_g2_workspaces=0,
            g5_store_detected=g5_detected,
            g5_chat_sessions=g5_sessions,
            cursor_server_detected=server,
        )

    def _wsl_probe(self, context: DiscoveryContext) -> _Probe:
        """Spec section 1: WSL fails closed; a visible host store adds a hint."""
        root = context.home / ".config" / "Cursor" / "User"
        code = "SOURCE_WSL_HOST_STORE_HINT" if _windows_host_store_seen() else "SOURCE_NOT_FOUND"
        return _Probe(
            root=root,
            accessibility=Accessibility.NOT_FOUND,
            diagnostic_code=code,
            fingerprint="absent",
            estimated_records=0,
            earliest=None,
            latest=None,
            candidate_messages=0,
            candidate_words=0,
            candidate_bytes=0,
            stats=self._empty_stats(),
        )

    def _degenerate_probe(
        self,
        root: Path,
        home: Path,
        *,
        accessibility: Accessibility,
        diagnostic_code: str | None,
        fingerprint: str,
    ) -> _Probe:
        g5_detected, g5_sessions = self._probe_g5(home)
        server = (home / ".cursor-server").is_dir()
        return _Probe(
            root=root,
            accessibility=accessibility,
            diagnostic_code=diagnostic_code,
            fingerprint=fingerprint,
            estimated_records=0,
            earliest=None,
            latest=None,
            candidate_messages=0,
            candidate_words=0,
            candidate_bytes=0,
            stats=self._empty_stats(
                g5_detected=g5_detected, g5_sessions=g5_sessions, server=server
            ),
        )

    def _probe_root(self, root: Path, home: Path) -> _Probe:
        global_db = root / "globalStorage" / "state.vscdb"
        if not global_db.is_file():
            # Spec 10.7: an untested server-side store is detected, never read.
            if (home / ".cursor-server").is_dir():
                return self._degenerate_probe(
                    root,
                    home,
                    accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                    diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                    fingerprint="cursor-server",
                )
            return self._degenerate_probe(
                root,
                home,
                accessibility=Accessibility.NOT_FOUND,
                diagnostic_code="SOURCE_NOT_FOUND",
                fingerprint="absent",
            )
        try:
            database = self._open_state_db(global_db)
        except sqlite3.OperationalError:
            return self._degenerate_probe(
                root,
                home,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        except sqlite3.DatabaseError:
            return self._degenerate_probe(
                root,
                home,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="not-a-database",
            )
        try:
            if not database.table_names() >= REQUIRED_TABLES:
                return self._degenerate_probe(
                    root,
                    home,
                    accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                    diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                    fingerprint="missing-tables",
                )
            scan = scan_global_store(database)
        except sqlite3.DatabaseError:
            return self._degenerate_probe(
                root,
                home,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="not-a-database",
            )
        finally:
            database.close()

        stats = self._collect_stats(root, home, scan)
        return self._classify(root, scan, stats)

    def _collect_stats(self, root: Path, home: Path, scan: GlobalStoreScan) -> CursorInventoryStats:
        indexed = 0
        links = 0
        folder_hashes: list[str] = []
        legacy_g1 = 0
        legacy_g2 = 0
        workspace_root = root / "workspaceStorage"
        if workspace_root.is_dir():
            try:
                entries = sorted(workspace_root.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_dir() or entry.is_symlink():
                    continue
                workspace_db = entry / "state.vscdb"
                if workspace_db.is_file():
                    try:
                        database = self._open_state_db(workspace_db)
                    except sqlite3.Error:
                        continue
                    try:
                        composer_ids, inline = parse_workspace_index(
                            database.item_value(WORKSPACE_INDEX_KEY)
                        )
                        has_g1 = database.item_present(LEGACY_AICHAT_KEY)
                    except sqlite3.Error:
                        continue
                    finally:
                        database.close()
                    indexed += 1
                    links += len(composer_ids)
                    if inline:
                        legacy_g2 += 1
                    if has_g1:
                        legacy_g1 += 1
                folder_hash = self._workspace_folder_hash(entry)
                if folder_hash is not None:
                    folder_hashes.append(folder_hash)
        g5_detected, g5_sessions = self._probe_g5(home)
        return CursorInventoryStats(
            scan=scan,
            workspaces_indexed=indexed,
            workspace_composer_links=links,
            workspace_folder_hashes=tuple(folder_hashes),
            legacy_g1_workspaces=legacy_g1,
            legacy_g2_workspaces=legacy_g2,
            g5_store_detected=g5_detected,
            g5_chat_sessions=g5_sessions,
            cursor_server_detected=(home / ".cursor-server").is_dir(),
        )

    def _workspace_folder_hash(self, workspace_dir: Path) -> str | None:
        """Spec section 3: the ``folder`` value goes only into a hash."""
        meta_path = workspace_dir / "workspace.json"
        if not meta_path.is_file():
            return None
        self._assert_allowed_file(meta_path)
        self._opened_paths.append(meta_path)
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        folder = payload.get("folder") if isinstance(payload, dict) else None
        if not isinstance(folder, str) or not folder:
            return None
        return _hash_text(folder)

    def _probe_g5(self, home: Path) -> tuple[bool, int]:
        """G5 CLI-store presence: directory existence and names only."""
        chats = home / ".cursor" / "chats"
        if not chats.is_dir():
            return False, 0
        sessions = 0
        try:
            for hash_dir in chats.iterdir():
                if not hash_dir.is_dir():
                    continue
                sessions += sum(1 for child in hash_dir.iterdir() if child.is_dir())
        except OSError:
            return True, sessions
        return True, sessions

    def _classify(self, root: Path, scan: GlobalStoreScan, stats: CursorInventoryStats) -> _Probe:
        unsupported = (
            scan.malformed_bubbles_over_threshold
            or scan.unsupported_bubbles_dominate
            or scan.wrapper_leaks > 0
            or (scan.composers_total > 0 and scan.composers_g4 == 0)
            or (scan.composers_total == 0 and _legacy_material_present(stats))
        )
        if unsupported:
            # Detected, unsupported schema: inventoried, no candidate counts.
            return _Probe(
                root=root,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint=_fingerprint(stats),
                estimated_records=scan.composers_total,
                earliest=None,
                latest=None,
                candidate_messages=0,
                candidate_words=0,
                candidate_bytes=0,
                stats=stats,
            )
        return _Probe(
            root=root,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            fingerprint=_fingerprint(stats),
            estimated_records=scan.composers_total,
            earliest=scan.earliest,
            latest=scan.latest,
            candidate_messages=scan.candidate_messages,
            candidate_words=scan.candidate_words,
            candidate_bytes=scan.candidate_bytes,
            stats=stats,
        )

    def _build_record(
        self, context: DiscoveryContext, probe: _Probe, *, position: int
    ) -> SourceInstanceRecord:
        path_hash = _hash_path(probe.root)
        return SourceInstanceRecord(
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            instance_key=path_hash,
            opaque_label=f"{_HUMAN_NAME} {position}",
            storage_format=_STORAGE_FORMAT,
            schema_fingerprint=probe.fingerprint,
            path_hash=path_hash,
            os_environment=context.os_environment,
            app_version=None,
            stability=Stability.BETA,
            accessibility=probe.accessibility,
            diagnostic_code=probe.diagnostic_code,
            estimated_records=probe.estimated_records,
            earliest_timestamp=probe.earliest,
            latest_timestamp=probe.latest,
            candidate_messages=probe.candidate_messages,
            candidate_words=probe.candidate_words,
            candidate_bytes=probe.candidate_bytes,
        )

    def inventory_stats(self, instance_key: str) -> CursorInventoryStats | None:
        """Accounting for the last discovery of one instance, if any."""
        return self._inventory_stats.get(instance_key)

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        ensure_private_dir(target_dir)
        global_db = source_path / "globalStorage" / "state.vscdb"
        self._preflight_free_space(global_db, target_dir)
        entries: list[SnapshotFileEntry] = []
        source_hashes: dict[str, str] = {}
        entries.append(
            self._backup_database(
                global_db, target_dir / "globalStorage" / "state.vscdb", _GLOBAL_DB_RELATIVE
            )
        )
        source_hashes[_GLOBAL_DB_RELATIVE] = _hash_path(global_db)

        workspace_hashes: dict[str, str | None] = {}
        workspace_root = source_path / "workspaceStorage"
        if workspace_root.is_dir():
            for entry in sorted(workspace_root.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                workspace_db = entry / "state.vscdb"
                if not workspace_db.is_file():
                    continue
                relative = f"workspaceStorage/{entry.name}/state.vscdb"
                entries.append(
                    self._backup_database(
                        workspace_db,
                        target_dir / "workspaceStorage" / entry.name / "state.vscdb",
                        relative,
                    )
                )
                source_hashes[relative] = _hash_path(workspace_db)
                workspace_hashes[entry.name] = self._workspace_folder_hash(entry)

        payload = json.dumps(
            {
                "reason": BETA_NO_TEXT_REASON,
                "source_path_hashes": source_hashes,
                "workspace_folder_hashes": workspace_hashes,
            },
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        with contextlib.suppress(OSError):
            meta_path.chmod(_POSIX_MODE_FILE)
        entries.append(
            SnapshotFileEntry(
                relative_path=_SNAPSHOT_META_NAME,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    def _preflight_free_space(self, source: Path, target_dir: Path) -> None:
        """Spec 8.2: never start a snapshot that cannot finish."""
        required = source.stat().st_size
        wal = source.with_name(source.name + "-wal")
        if wal.is_file():
            required += wal.stat().st_size
        free = shutil.disk_usage(target_dir).free
        if free < required + required // 10 + _FREE_SPACE_MARGIN_BYTES:
            msg = (
                "insufficient free space for the Cursor snapshot: "
                f"need about {required} bytes plus margin, have {free}"
            )
            raise OSError(msg)

    def _backup_database(self, source: Path, target: Path, relative: str) -> SnapshotFileEntry:
        """SQLite backup API from a read-only connection (spec 4.6 and 8.2)."""
        self._assert_allowed_file(source)
        self._opened_paths.append(source)
        ensure_private_dir(target.parent)
        quoted = source.resolve().as_posix()
        last_error: sqlite3.Error | None = None
        for attempt in range(_BACKUP_ATTEMPTS):
            source_connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
            target_connection = sqlite3.connect(str(target))
            try:
                source_connection.execute("PRAGMA busy_timeout = 5000")
                source_connection.backup(target_connection)
                last_error = None
            except sqlite3.Error as error:
                last_error = error
            finally:
                target_connection.close()
                source_connection.close()
            if last_error is None:
                break
            if attempt + 1 < _BACKUP_ATTEMPTS:
                time.sleep(_BACKUP_RETRY_SLEEP_SECONDS)
        if last_error is not None:
            msg = (
                f"could not snapshot the Cursor database after {_BACKUP_ATTEMPTS} attempts; "
                f"close Cursor and retry ({last_error})"
            )
            raise OSError(msg)
        with contextlib.suppress(OSError):
            target.chmod(_POSIX_MODE_FILE)
        digest, size = self._hash_file(target)
        return SnapshotFileEntry(relative_path=relative, size_bytes=size, sha256=digest)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        """Scan the snapshot for count verification; contribute no text.

        Cursor is beta because rawness is unknown (spec sections 5 and 10.1);
        under project specification 4.7 an unknown variant may be inventoried
        but contributes no analyzable text, so this always yields nothing.
        """
        scan: GlobalStoreScan | None = None
        snapshot_db = snapshot_dir / "globalStorage" / "state.vscdb"
        if snapshot_db.is_file():
            try:
                database = self._open_state_db(snapshot_db)
            except sqlite3.Error:
                database = None
            if database is not None:
                try:
                    if database.table_names() >= REQUIRED_TABLES:
                        scan = scan_global_store(database)
                except sqlite3.DatabaseError:
                    scan = None
                finally:
                    database.close()
        self._extraction_stats[instance.instance_key] = CursorExtractionStats(
            reason=BETA_NO_TEXT_REASON, scan=scan
        )
        return iter(())

    def extraction_stats(self, instance_key: str) -> CursorExtractionStats | None:
        """Accounting for the last extraction of one instance, if any."""
        return self._extraction_stats.get(instance_key)

    # -- verification ------------------------------------------------------

    def verify(
        self,
        instance: SourceInstanceRecord,
        utterances: list[NormalizedUtterance],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for utterance in utterances:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "beta Cursor adapter must not contribute analyzable text "
                    "(rawness unknown, project specification 4.7)",
                    item_ref=utterance.utterance_id,
                )
            )
        stats = self._extraction_stats.get(instance.instance_key)
        if stats is not None and stats.scan is not None:
            scan = stats.scan
            if (
                instance.accessibility is Accessibility.FOUND
                and scan.candidate_messages != instance.candidate_messages
            ):
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        f"snapshot re-scan counted {scan.candidate_messages} candidate "
                        f"messages but discovery counted {instance.candidate_messages}",
                        item_ref=instance.instance_key,
                    )
                )
            if scan.wrapper_leaks > 0:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "bubble text contains an injected wrapper tag; the store schema "
                        "changed and the instance fails closed",
                        item_ref=instance.instance_key,
                    )
                )
            if scan.fidelity_checked > 0 and scan.fidelity_mismatches * 3 > scan.fidelity_checked:
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "richText fidelity cross-check failed beyond the mention and "
                        "code-fence tolerance",
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
        for kind, key in self._key_audit:
            if not _key_access_allowed(kind, key):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        f"a non-allowlisted database access appears in the key audit: {kind}",
                    )
                )
        return diagnostics


def _key_access_allowed(kind: str, key: str) -> bool:
    if kind in {"kv_range", "kv_get"}:
        return key.startswith(ALLOWED_KV_PREFIXES)
    if kind == "item_get":
        return key in ALLOWED_ITEM_VALUE_KEYS
    if kind == "item_present":
        return key in ALLOWED_ITEM_PRESENCE_KEYS or key in ALLOWED_ITEM_VALUE_KEYS
    return False


def create_adapter() -> SourceAdapter:
    """Factory used by the discovery registry."""
    return CursorAdapter()
