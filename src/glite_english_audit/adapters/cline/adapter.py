"""The Cline (VS Code extension, standalone core, SDK) source adapter.

Discovers the two storage families from ``specifications/sources/cline.md``:
family A editor ``globalStorage`` roots (a fixed per-platform list of known
editor data directories crossed with the ``saoudrizwan.claude-dev`` extension
ID) and the family B Cline data directory (``CLINE_DATA_DIR``, else
``CLINE_DIR``/data, else ``<home>/.cline/data``). Every existing root is one
source instance; ``tasks/``, ``data/tasks/``, and ``sessions/`` are probed
under each root.

The adapter opens only the allowlisted conversation artifacts: per task
``api_conversation_history.json`` (extraction) and ``ui_messages.json`` or
its G1 name ``claude_messages.json`` (structure-only); per session the
``<sessionId>.json`` manifest (eligibility/timestamps) and
``*.messages.json`` payloads. Everything else in the stores — secrets,
settings, state indexes, SQLite databases, checkpoints — is denylisted and
never opened (spec section 2). Discovery never returns, prints, or logs
source text.
"""

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.cline.records import (
    API_HISTORY_NAME,
    LEGACY_UI_MESSAGES_NAME,
    MAX_CONVERSATION_FILE_BYTES,
    MESSAGES_SUFFIX,
    UI_MESSAGES_NAME,
    OversizedFileError,
    UnitCandidate,
    UnitScan,
    UnitStatus,
    normalize_whitespace,
    scan_session_dir,
    scan_task_dir,
)
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
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

ADAPTER_ID = "cline"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Cline"
_STORAGE_FORMAT = "json"
_EXTENSION_DIR = "saoudrizwan.claude-dev"
_SNAPSHOT_META_NAME = "cline-source-paths.json"
_DATA_DIR_ENV = "CLINE_DATA_DIR"
_DIR_ENV = "CLINE_DIR"

# Spec 1: the fixed list of known editor data roots. Unknown editors are
# never guessed.
_EDITOR_DIR_NAMES = ("Code", "Code - Insiders", "VSCodium", "Code - OSS", "Cursor", "Windsurf")

# Fixed DrvFS mount root for Windows-host stores visible from WSL (spec 1).
# The one deliberate absolute path in this adapter; tests repoint it.
_WSL_MOUNT_BASE = Path("/mnt")

# Spec section 2: files and directories that must never be opened. The
# per-open allowlist already excludes them; this is defense in depth plus the
# audit anchor for tests.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        "secrets.json",
        "globalState.json",
        "taskHistory.json",
        "sessions.index.json",
        "subagent-spawn-queue.json",
        "cline_mcp_settings.json",
        "providers.json",
        "global-settings.json",
        "settings.json",
        "context_history.json",
        "task_metadata.json",
        "hooks.jsonl",
        "sessions.db",
        "connectors.db",
        "cron.db",
        "locks.db",
        "state.vscdb",
        ".clineignore",
        ".clinerules",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset(
    {
        "checkpoints",
        "cache",
        "db",
        "settings",
        "state",
        "workspaces",
        "teams",
        "connectors",
        "logs",
    }
)

_MAX_TIMESTAMP = datetime.max.replace(tzinfo=UTC)
_EARLIEST_PLAUSIBLE = datetime(2020, 1, 1, tzinfo=UTC)
_POSIX = os.name == "posix"

# Spec 6.4: no extracted utterance may still carry a wrapper or injected tag.
_FORBIDDEN_TEXT_MARKERS = (
    "<environment_details",
    "<file_content",
    "<folder_content",
    "<url_content",
    "<workspace_diagnostics",
    "<explicit_instructions",
    "<user_input",
    "<mode_notice",
    "<user_command",
    "<task>",
    "</task>",
    "<feedback>",
    "</feedback>",
    "<answer>",
    "</answer>",
    "<user_message>",
    "</user_message>",
)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _editor_data_bases(context: DiscoveryContext) -> list[Path]:
    """Per-platform editor data directories holding ``User/globalStorage``."""
    home = context.home
    env = context.os_environment
    if env is OsEnvironment.MACOS:
        support = home / "Library" / "Application Support"
        bases = [support / name for name in _EDITOR_DIR_NAMES]
    elif env is OsEnvironment.WINDOWS:
        appdata_value = context.environ.get("APPDATA", "").strip()
        appdata = Path(appdata_value) if appdata_value else home / "AppData" / "Roaming"
        bases = [appdata / name for name in _EDITOR_DIR_NAMES]
    else:
        # Native Linux and WSL share the XDG convention inside the (WSL) home.
        config = home / ".config"
        bases = [config / name for name in _EDITOR_DIR_NAMES]
        if env is OsEnvironment.WSL:
            bases.append(home / ".vscode-server" / "data")
    if env is not OsEnvironment.WINDOWS:
        bases.append(home / ".local" / "share" / "code-server")
    return bases


def _family_b_home(context: DiscoveryContext) -> Path:
    """Home for the family B default; Windows prefers HOME (spec 1, E8)."""
    if context.os_environment is not OsEnvironment.WINDOWS:
        return context.home
    for key in ("HOME", "USERPROFILE"):
        value = context.environ.get(key, "").strip()
        if value:
            return Path(value)
    drive = context.environ.get("HOMEDRIVE", "").strip()
    tail = context.environ.get("HOMEPATH", "").strip()
    if drive and tail:
        return Path(drive + tail)
    return context.home


def _family_b_root(context: DiscoveryContext) -> tuple[Path, bool]:
    """Resolved Cline data dir plus whether an env override selected it."""
    data_override = context.environ.get(_DATA_DIR_ENV, "").strip()
    if data_override:
        return Path(data_override), True
    dir_override = context.environ.get(_DIR_ENV, "").strip()
    if dir_override:
        return Path(dir_override) / "data", True
    return _family_b_home(context) / ".cline" / "data", False


def _windows_host_store_visible() -> bool:
    """A Cline store on a mounted Windows drive is visible from WSL."""
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
            # Fixed-layout checks only; profile directories are never walked.
            if (profile / ".cline" / "data").is_dir():
                return True
            editor_root = (
                profile / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / _EXTENSION_DIR
            )
            if editor_root.is_dir():
                return True
    return False


@dataclass
class _RootScan:
    """Every unit under one instance root plus the deduplicated candidates."""

    units: list[UnitScan]
    fingerprint: str
    deduped: list[tuple[UnitScan, UnitCandidate]]


@dataclass(frozen=True)
class _ProvisionalInstance:
    """One root's aggregates before opaque-label assignment."""

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


def _degenerate_provisional(
    root: Path,
    *,
    accessibility: Accessibility,
    diagnostic_code: str | None,
    fingerprint: str,
) -> _ProvisionalInstance:
    return _ProvisionalInstance(
        root=root,
        path_hash=_hash_text(_canonical_path(root)),
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


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification and tests."""

    utterance_count: int
    units_scanned: int
    unit_statuses: tuple[tuple[str, str], ...]
    counter_totals: tuple[tuple[str, int], ...]


class ClineAdapter:
    """SourceAdapter implementation for the Cline conversation stores."""

    def __init__(self) -> None:
        self._opened_paths: list[Path] = []
        self._extraction_stats: dict[str, ExtractionStats] = {}
        self._discovery_diagnostics: list[Diagnostic] = []

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def stability(self) -> Stability:
        return Stability.STABLE

    def discovery_diagnostics(self) -> list[Diagnostic]:
        """Aggregate-only diagnostics from the last discover() call."""
        return list(self._discovery_diagnostics)

    # -- file access guards ------------------------------------------------

    def _assert_allowlisted(self, path: Path) -> None:
        """Refuse any open outside the spec section 2 allowlist."""
        name = path.name
        if (
            name in DENY_FILE_NAMES
            or name.startswith("remote_config_")
            or name.startswith("state.vscdb")
            or any(part in DENY_DIR_NAMES for part in path.parts)
        ):
            msg = "refusing to open a denylisted path"
            raise PermissionError(msg)
        if name in (API_HISTORY_NAME, UI_MESSAGES_NAME, LEGACY_UI_MESSAGES_NAME):
            return
        if name.endswith(MESSAGES_SUFFIX):
            return
        if name == f"{path.parent.name}.json" and path.parent.parent.name == "sessions":
            return
        if name == _SNAPSHOT_META_NAME:
            return
        msg = "refusing to open a path outside the conversation allowlist"
        raise PermissionError(msg)

    def _read_allowlisted_bytes(self, path: Path) -> bytes:
        self._assert_allowlisted(path)
        if path.stat().st_size > MAX_CONVERSATION_FILE_BYTES:
            raise OversizedFileError(path.name)
        self._opened_paths.append(path)
        with path.open("rb") as handle:
            return handle.read()

    # -- root scanning -----------------------------------------------------

    def _scan_root(self, root: Path) -> _RootScan:
        units: list[UnitScan] = []
        for tasks_dir in (root / "tasks", root / "data" / "tasks"):
            if not tasks_dir.is_dir():
                continue
            for task_dir in sorted(tasks_dir.iterdir()):
                if not task_dir.is_dir() or task_dir.is_symlink():
                    continue
                relpath = task_dir.relative_to(root).as_posix()
                units.append(scan_task_dir(task_dir, relpath, self._read_allowlisted_bytes))
        sessions_dir = root / "sessions"
        if sessions_dir.is_dir():
            for session_dir in sorted(sessions_dir.iterdir()):
                # Index and lock files at this level are skipped, never opened.
                if not session_dir.is_dir() or session_dir.is_symlink():
                    continue
                relpath = session_dir.relative_to(root).as_posix()
                units.append(scan_session_dir(session_dir, relpath, self._read_allowlisted_bytes))

        # Spec 4.2 new_task mitigation, instance-wide.
        arguments = {
            normalize_whitespace(argument) for unit in units for argument in unit.new_task_arguments
        }
        if arguments:
            for unit in units:
                kept: list[UnitCandidate] = []
                for candidate in unit.candidates:
                    if (
                        candidate.wrapper_kind == "task"
                        and normalize_whitespace(candidate.text) in arguments
                    ):
                        unit.bump("subtask_initial_message")
                    else:
                        kept.append(candidate)
                unit.candidates = kept

        # Spec 5: cross-file exact-hash dedup; the earliest timestamp wins.
        ordered = sorted(
            ((unit, candidate) for unit in units for candidate in unit.candidates),
            key=lambda pair: (
                pair[1].timestamp or _MAX_TIMESTAMP,
                pair[0].unit_relpath,
                pair[1].record_ref,
            ),
        )
        seen_hashes: set[str] = set()
        deduped: list[tuple[UnitScan, UnitCandidate]] = []
        for unit, candidate in ordered:
            if candidate.text_hash in seen_hashes:
                continue
            seen_hashes.add(candidate.text_hash)
            deduped.append((unit, candidate))

        layouts = sorted(
            {unit.generation for unit in units if unit.status is not UnitStatus.MALFORMED}
        )
        parts = layouts if layouts else []
        if (root / "state" / "taskHistory.json").is_file():
            parts = [*parts, "state-index"]
        if (root / "db").is_dir():
            parts = [*parts, "db"]
        fingerprint = "+".join(parts) if units else "empty"
        return _RootScan(units=units, fingerprint=fingerprint, deduped=deduped)

    def _probe_root(self, root: Path) -> _ProvisionalInstance:
        if not root.is_dir():
            return _degenerate_provisional(
                root,
                accessibility=Accessibility.NOT_FOUND,
                diagnostic_code="SOURCE_NOT_FOUND",
                fingerprint="absent",
            )
        try:
            scan = self._scan_root(root)
        except OSError:
            return _degenerate_provisional(
                root,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        healthy = any(
            unit.status in (UnitStatus.SUPPORTED, UnitStatus.EXCLUDED) for unit in scan.units
        )
        if scan.units and not healthy:
            # Spec 7: every unit is unsupported or malformed.
            return _degenerate_provisional(
                root,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="unsupported",
            )
        stamps = [
            candidate.timestamp for _, candidate in scan.deduped if candidate.timestamp is not None
        ]
        return _ProvisionalInstance(
            root=root,
            path_hash=_hash_text(_canonical_path(root)),
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=scan.fingerprint,
            estimated_records=sum(
                unit.record_count for unit in scan.units if unit.status is UnitStatus.SUPPORTED
            ),
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            messages=len(scan.deduped),
            words=sum(count_words(candidate.text) for _, candidate in scan.deduped),
            bytes_count=sum(len(candidate.text.encode("utf-8")) for _, candidate in scan.deduped),
        )

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        self._discovery_diagnostics = []
        provisional: list[_ProvisionalInstance] = []
        seen_roots: set[str] = set()

        def add_probe(probe: _ProvisionalInstance) -> None:
            if probe.path_hash not in seen_roots:
                seen_roots.add(probe.path_hash)
                provisional.append(probe)

        for base in _editor_data_bases(context):
            root = base / "User" / "globalStorage" / _EXTENSION_DIR
            if root.is_dir():
                add_probe(self._probe_root(root))

        family_b, overridden = _family_b_root(context)
        if family_b.is_dir():
            add_probe(self._probe_root(family_b))
        elif overridden:
            # Spec 7: a missing override is reported; the default is never
            # probed as a fallback.
            add_probe(
                _degenerate_provisional(
                    family_b,
                    accessibility=Accessibility.NOT_FOUND,
                    diagnostic_code="SOURCE_NOT_FOUND",
                    fingerprint="absent",
                )
            )

        if context.os_environment is OsEnvironment.WSL and _windows_host_store_visible():
            self._discovery_diagnostics.append(
                Diagnostic.from_code(
                    "SOURCE_WSL_HOST_STORE_HINT",
                    "A Windows-host Cline store is visible from WSL; run the audit "
                    "from native Windows to include it.",
                )
            )

        if not provisional:
            add_probe(
                _degenerate_provisional(
                    family_b,
                    accessibility=Accessibility.NOT_FOUND,
                    diagnostic_code="SOURCE_NOT_FOUND",
                    fingerprint="absent",
                )
            )

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

    # -- snapshot ----------------------------------------------------------

    def _unit_files(self, root: Path) -> list[Path]:
        """Every allowlisted conversation file under one instance root."""
        files: list[Path] = []
        for tasks_dir in (root / "tasks", root / "data" / "tasks"):
            if not tasks_dir.is_dir():
                continue
            for task_dir in sorted(tasks_dir.iterdir()):
                if not task_dir.is_dir() or task_dir.is_symlink():
                    continue
                for name in (API_HISTORY_NAME, UI_MESSAGES_NAME, LEGACY_UI_MESSAGES_NAME):
                    candidate = task_dir / name
                    if candidate.is_file() and not candidate.is_symlink():
                        files.append(candidate)
        sessions_dir = root / "sessions"
        if sessions_dir.is_dir():
            for session_dir in sorted(sessions_dir.iterdir()):
                if not session_dir.is_dir() or session_dir.is_symlink():
                    continue
                manifest = session_dir / f"{session_dir.name}.json"
                if manifest.is_file() and not manifest.is_symlink():
                    files.append(manifest)
                for entry in sorted(session_dir.iterdir()):
                    if (
                        entry.is_file()
                        and not entry.is_symlink()
                        and entry.name.endswith(MESSAGES_SUFFIX)
                    ):
                        files.append(entry)
        return files

    def _copy_bytes(self, source: Path, target: Path) -> tuple[str, int]:
        self._assert_allowlisted(source)
        self._opened_paths.append(source)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _copy_parses(target: Path) -> bool:
        try:
            json.loads(target.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        return True

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        ensure_private_dir(target_dir)
        entries: list[SnapshotFileEntry] = []
        path_hashes: dict[str, str] = {}
        for source_file in self._unit_files(source_path):
            relative = source_file.relative_to(source_path)
            target_file = target_dir / relative
            ensure_private_dir(target_file.parent)
            digest, size = self._copy_bytes(source_file, target_file)
            if not self._copy_parses(target_file):
                # Producers rewrite files whole; a mid-rewrite copy can be
                # truncated. Retry once, then keep the copy: extraction
                # treats an unparseable snapshot copy as malformed_file.
                digest, size = self._copy_bytes(source_file, target_file)
            if _POSIX:
                target_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            source_stat = source_file.stat()
            os.utime(target_file, (source_stat.st_atime, source_stat.st_mtime))
            entries.append(
                SnapshotFileEntry(relative_path=relative.as_posix(), size_bytes=size, sha256=digest)
            )
            path_hashes[relative.as_posix()] = _hash_text(_canonical_path(source_file))
        payload = json.dumps({"source_path_hashes": path_hashes}, sort_keys=True, indent=2).encode(
            "utf-8"
        )
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
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        path_hashes = self._read_snapshot_meta(snapshot_dir)
        scan = self._scan_root(snapshot_dir)
        for unit, candidate in scan.deduped:
            session_hash = _hash_text(unit.unit_id)
            source_path_hash = instance.path_hash
            if unit.source_file_relpath is not None:
                source_path_hash = path_hashes.get(unit.source_file_relpath, instance.path_hash)
            yield NormalizedUtterance(
                utterance_id=f"{ADAPTER_ID}-{session_hash[:16]}-{candidate.record_ref}",
                source_adapter=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                session_hash=session_hash,
                timestamp=candidate.timestamp,
                text=candidate.text,
                modality=Modality.WRITTEN,
                text_status=candidate.text_status,
                authorship_confidence=candidate.authorship_confidence,
                authorship_basis=candidate.authorship_basis,
                source_path_hash=source_path_hash,
                destination_app=None,
                content_flags=list(candidate.content_flags),
            )
        counter_totals: dict[str, int] = {}
        for unit in scan.units:
            for name, value in unit.counters.items():
                counter_totals[name] = counter_totals.get(name, 0) + value
        self._extraction_stats[instance.instance_key] = ExtractionStats(
            utterance_count=len(scan.deduped),
            units_scanned=len(scan.units),
            unit_statuses=tuple((unit.unit_relpath, unit.status.value) for unit in scan.units),
            counter_totals=tuple(sorted(counter_totals.items())),
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
                        "duplicate utterance ID after deduplication",
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
                        "utterance does not belong to the cline adapter",
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
            if any(marker in utterance.text for marker in _FORBIDDEN_TEXT_MARKERS):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text contains a wrapper or injected tag",
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
    return ClineAdapter()
