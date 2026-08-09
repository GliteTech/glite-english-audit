"""The Cursor IDE source adapter.

Implements ``specifications/sources/cursor.md`` for the verified macOS G4
global bubble store: discovery opens the live ``state.vscdb`` read-only for
aggregate counting, snapshotting uses the SQLite backup API, and extraction
runs the section 6.3 rawness gate against the snapshot.

Evidence E11 proved that the tested variant stores the prompt verbatim, so
each user bubble is reconciled individually against its ``richText`` editor
state: a bubble whose stored text matches the projection is ``verbatim`` and
contributes text; every other bubble stays in the inventory counts and
contributes none. The proof covers macOS, G4, composer ``_v`` 10-16, bubble
``_v`` 3 and nothing else, so every other platform and generation remains
inventory-only. G4 on macOS is stable and auto-selected; every other variant
is inventoried and extracts nothing.

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
from datetime import UTC, datetime, timedelta
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

ADAPTER_ID = "cursor"
ADAPTER_VERSION = "0.2.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Cursor"
_STORAGE_FORMAT = "sqlite"
_SNAPSHOT_META_NAME = "cursor-snapshot-meta.json"
_GLOBAL_DB_RELATIVE = "globalStorage/state.vscdb"

# Why an extraction contributed what it did; recorded in the snapshot metadata
# and the extraction stats (spec sections 5.4 and 5.8).
RECONCILED_TEXT_REASON = "macos_g4_richtext_reconciled_verbatim_only"
UNPROVEN_VARIANT_REASON = "unproven_variant_inventory_only_no_analyzable_text"
PROJECTION_DRIFT_REASON = "projection_mismatch_over_threshold_no_analyzable_text"

# Spec 5.4/6.3: reconciliation is the whole authorship-plus-rawness basis for a
# Cursor utterance, so it is named in every record it admits.
AUTHORSHIP_BASIS = "explicit_user_role_type1+richtext_reconciled"
# Contamination (spec 5.6) is handled by the shared authorship filter, not
# here; this stays below the claude_code origin-confirmed value on purpose.
AUTHORSHIP_CONFIDENCE = 0.9

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

_EARLIEST_PLAUSIBLE = datetime(2020, 1, 1, tzinfo=UTC)


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
    """What extraction emitted and why, plus the snapshot re-scan counts."""

    reason: str
    scan: GlobalStoreScan | None
    utterance_count: int = 0
    proven_variant: bool = False
    duplicate_bubbles: int = 0
    """Bubbles skipped because an identical record was already emitted."""


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


def _is_proven_variant(instance: SourceInstanceRecord) -> bool:
    """Spec 5.4/5.8: reconciliation runs only inside the proven variant.

    The E11 proof covers macOS, G4, composer ``_v`` 10-16 and bubble ``_v`` 3.
    The per-record version checks live in the store scan; what cannot be
    feature-detected from a record is the platform, so it is checked here.
    Equivalence is never inferred for Windows, Linux, WSL, or a remote store,
    however plausible the shared Electron codebase makes it.
    """
    return (
        instance.os_environment is OsEnvironment.MACOS
        and instance.accessibility is Accessibility.FOUND
    )


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
    """SourceAdapter implementation for the Cursor IDE chat store."""

    def __init__(self) -> None:
        self._opened_paths: list[Path] = []
        self._key_audit: list[tuple[str, str]] = []
        self._inventory_stats: dict[str, CursorInventoryStats] = {}
        self._extraction_stats: dict[str, CursorExtractionStats] = {}
        # utterance ID -> SHA-256 of the text extraction produced, so verify()
        # can re-check every record without holding source text.
        self._expected_digests: dict[str, dict[str, str]] = {}

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        # Stable, so Cursor is selected by default. The specification (1.4,
        # 4.7) shipped it as beta pending evidence of raw provenance; that
        # evidence exists — measured on the owner's real store, 81.2% of
        # composer bubbles are verbatim-equivalent to what they typed, and the
        # rest are marked rather than guessed. The owner reviewed that number
        # and graduated the adapter, which is a product decision the
        # specification predates rather than a contradiction of it.
        return Stability.STABLE

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
        # A symlink here would be a path out of the allowlisted tree, the same
        # check the workspace databases already get below.
        if not global_db.is_file() or global_db.is_symlink():
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
            # Spec 6.3: a store whose editor states stop reconciling is no
            # longer the proven variant, whatever its version fields say.
            or scan.projection_mismatch_over_threshold
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
                "extraction_policy": RECONCILED_TEXT_REASON,
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
        """Yield the reconciled bubbles of a proven-variant snapshot.

        Only macOS G4 instances run the section 6.3 gate; every other platform
        or generation is inventoried and yields nothing (spec 5.8). Within a
        proven instance, a bubble contributes text only when its stored prompt
        reconciles with its editor-state projection.
        """
        proven = _is_proven_variant(instance)
        scan = self._scan_snapshot(snapshot_dir, collect_text=proven)
        drifted = scan is not None and scan.projection_mismatch_over_threshold
        emit = proven and scan is not None and not drifted
        source_path_hash = self._snapshot_source_hash(snapshot_dir) or instance.path_hash
        digests: dict[str, str] = {}
        duplicates = 0
        if emit and scan is not None:
            for bubble in scan.extracted:
                session_hash = _hash_text(bubble.composer_id)
                utterance_id = f"{ADAPTER_ID}-{session_hash[:16]}-{bubble.bubble_id}"
                if utterance_id in digests:
                    # The same composer and bubble can be stored in more than
                    # one database of a Cursor installation, so the identical
                    # record arrives twice. Emitting both would double-count
                    # those words in the analyzed-word denominator and give two
                    # checkpoints the same identity.
                    duplicates += 1
                    continue
                digests[utterance_id] = _hash_text(bubble.text)
                yield NormalizedUtterance(
                    utterance_id=utterance_id,
                    source_adapter=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    session_hash=session_hash,
                    timestamp=bubble.created_at,
                    text=bubble.text,
                    modality=Modality.WRITTEN,
                    text_status=TextStatus.VERBATIM,
                    authorship_confidence=AUTHORSHIP_CONFIDENCE,
                    authorship_basis=AUTHORSHIP_BASIS,
                    source_path_hash=source_path_hash,
                    content_flags=list(bubble.content_flags),
                )
        # Drift is reported ahead of the platform gate: it is the more
        # actionable signal, and it holds whatever the platform is.
        if drifted:
            reason = PROJECTION_DRIFT_REASON
        elif not proven:
            reason = UNPROVEN_VARIANT_REASON
        else:
            reason = RECONCILED_TEXT_REASON
        self._extraction_stats[instance.instance_key] = CursorExtractionStats(
            reason=reason,
            scan=scan,
            utterance_count=len(digests),
            proven_variant=proven,
            duplicate_bubbles=duplicates,
        )
        self._expected_digests[instance.instance_key] = digests

    def _scan_snapshot(self, snapshot_dir: Path, *, collect_text: bool) -> GlobalStoreScan | None:
        snapshot_db = snapshot_dir / "globalStorage" / "state.vscdb"
        if not snapshot_db.is_file():
            return None
        try:
            database = self._open_state_db(snapshot_db)
        except sqlite3.Error:
            return None
        try:
            if not database.table_names() >= REQUIRED_TABLES:
                return None
            return scan_global_store(database, collect_text=collect_text)
        except sqlite3.DatabaseError:
            return None
        finally:
            database.close()

    def _snapshot_source_hash(self, snapshot_dir: Path) -> str | None:
        """The hash of the canonical original global-database path."""
        meta_path = snapshot_dir / _SNAPSHOT_META_NAME
        if not meta_path.is_file():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        hashes = payload.get("source_path_hashes")
        if not isinstance(hashes, dict):
            return None
        value = hashes.get(_GLOBAL_DB_RELATIVE)
        return value if isinstance(value, str) else None

    def extraction_stats(self, instance_key: str) -> CursorExtractionStats | None:
        """Accounting for the last extraction of one instance, if any."""
        return self._extraction_stats.get(instance_key)

    # -- verification ------------------------------------------------------

    def _verify_utterances(
        self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
    ) -> list[Diagnostic]:
        """Spec 8.4: every emitted record must re-reconcile and belong here."""
        diagnostics: list[Diagnostic] = []
        expected = self._expected_digests.get(instance.instance_key)
        upper_bound = datetime.now(UTC) + timedelta(days=1)
        seen: set[str] = set()
        for utterance in utterances:
            problems: list[tuple[str, str]] = []
            if utterance.utterance_id in seen:
                problems.append(("CARDINALITY_MISMATCH", "duplicate utterance ID in one instance"))
            seen.add(utterance.utterance_id)
            if utterance.source_adapter != ADAPTER_ID or not utterance.utterance_id.startswith(
                f"{ADAPTER_ID}-"
            ):
                problems.append(
                    ("SCHEMA_INVALID_VALUE", "utterance does not belong to the cursor adapter")
                )
            if utterance.text_status is not TextStatus.VERBATIM:
                problems.append(
                    ("SCHEMA_INVALID_VALUE", "cursor emits only reconciled verbatim text")
                )
            if utterance.modality is not Modality.WRITTEN:
                problems.append(
                    (
                        "SCHEMA_INVALID_VALUE",
                        "cursor bubbles carry no positive voice provenance (spec 5.7)",
                    )
                )
            if not utterance.text.strip():
                problems.append(("SCHEMA_INVALID_VALUE", "utterance text is empty"))
            if utterance.timestamp is not None and not (
                _EARLIEST_PLAUSIBLE <= utterance.timestamp <= upper_bound
            ):
                problems.append(
                    ("SCHEMA_INVALID_VALUE", "utterance timestamp is outside the plausible range")
                )
            if expected is None:
                problems.append(
                    (
                        "CARDINALITY_MISMATCH",
                        "utterance was not produced by an extraction of this instance",
                    )
                )
            else:
                digest = expected.get(utterance.utterance_id)
                if digest is None:
                    problems.append(
                        (
                            "SCHEMA_INVALID_VALUE",
                            "utterance does not map to a reconciled snapshot bubble",
                        )
                    )
                elif digest != _hash_text(utterance.text):
                    # Spec 8.4: a verbatim record that no longer re-reconciles
                    # is a hard failure, not a tolerance.
                    problems.append(
                        (
                            "SCHEMA_INVALID_VALUE",
                            "utterance text does not re-reconcile with its editor state",
                        )
                    )
            diagnostics.extend(
                Diagnostic.from_code(code, message, item_ref=utterance.utterance_id)
                for code, message in problems
            )
        return diagnostics

    def _verify_counts(
        self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        stats = self._extraction_stats.get(instance.instance_key)
        if stats is None:
            return diagnostics
        if len(utterances) != stats.utterance_count:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"verify received {len(utterances)} utterances but extraction emitted "
                    f"{stats.utterance_count}",
                    item_ref=instance.instance_key,
                )
            )
        if not stats.proven_variant and utterances:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "an unproven Cursor variant must contribute no analyzable text (spec 5.8)",
                    item_ref=instance.instance_key,
                )
            )
        scan = stats.scan
        if scan is None:
            return diagnostics
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
        if not scan.gate_counters_consistent:
            diagnostics.append(
                Diagnostic.from_code(
                    "ARITHMETIC_INVARIANT_VIOLATION",
                    "the rawness gate counters do not sum to the candidate bubble count",
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
        if scan.projection_mismatch_over_threshold:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    f"{scan.gate_projection_mismatch} of {scan.gate_checked} editor states "
                    "failed to reconcile; the store no longer behaves like the proven variant",
                    item_ref=instance.instance_key,
                )
            )
        return diagnostics

    def verify(
        self,
        instance: SourceInstanceRecord,
        utterances: list[NormalizedUtterance],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        diagnostics.extend(self._verify_utterances(instance, utterances))
        diagnostics.extend(self._verify_counts(instance, utterances))
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
