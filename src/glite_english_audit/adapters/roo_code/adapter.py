"""The Roo Code VS Code extension source adapter.

Discovers per-editor ``globalStorage`` extension stores (VS Code stable and
Insiders, VSCodium, Cursor, Windsurf, Antigravity, code-server, and the WSL
``.vscode-server`` layouts) for the ``rooveterinaryinc.roo-cline`` and
``rooveterinaryinc.roo-code-nightly`` extension directories, snapshots the
allowlisted per-task JSON files, and extracts wrapper-delimited user text per
``specifications/sources/roo_code.md``.

The adapter opens only ``api_conversation_history.json``,
``history_item.json``, and (structure-only) ``ui_messages.json`` inside task
directories. The allowlist is enforced before every open; ``settings/``,
``cache/``, ``checkpoints/``, ``task_metadata.json``, the editor's
``state.vscdb``, and every other store file are never opened. Discovery never
returns, prints, or logs source text.
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.roo_code.task_files import (
    API_HISTORY_NAME,
    HISTORY_ITEM_NAME,
    UI_MESSAGES_NAME,
    TaskScan,
    TaskStatus,
    apply_subtask_argument_match,
    normalized_text_sha256,
    scan_task,
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
from glite_english_audit.normalization.tokenizer import count_words

ADAPTER_ID = "roo_code"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Roo Code"
_STORAGE_FORMAT = "json"
_SNAPSHOT_META_NAME = "roo-code-session-hashes.json"

# Spec 6.1 large-file guard: upstream history bloat is a known failure mode.
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024

EXTENSION_DIR_NAMES = ("rooveterinaryinc.roo-cline", "rooveterinaryinc.roo-code-nightly")
# Spec 8.8: these editor variants are evidenced by maintainer issues only and
# stay beta at the instance level until the release smoke test covers them.
_STABLE_EDITOR_DIRS = ("Code", "Code - Insiders")
_BETA_EDITOR_DIRS = ("VSCodium", "Cursor", "Windsurf", "Antigravity")

# Spec section 2: never-open names. Enumeration is allowlist-driven, so these
# sets are a pre-open assertion and a test anchor, not the primary gate.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        "mcp_settings.json",
        "custom_modes.yaml",
        "custom_modes.json",
        "task_metadata.json",
        "claude_messages.json",
        "state.vscdb",
        "state.vscdb-journal",
        "state.vscdb-wal",
        "state.vscdb-shm",
        "settings.json",
        "roo-code-settings.json",
        ".roomodes",
        ".rooignore",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset({"settings", "cache", "checkpoints", ".roo"})
_ALLOWED_OPEN_NAMES: frozenset[str] = frozenset(
    {API_HISTORY_NAME, HISTORY_ITEM_NAME, UI_MESSAGES_NAME, _SNAPSHOT_META_NAME}
)
_SNAPSHOT_FILE_NAMES = (API_HISTORY_NAME, HISTORY_ITEM_NAME, UI_MESSAGES_NAME)

# Fixed DrvFS mount root for Windows-host stores visible from WSL (spec 1).
# The one deliberate absolute path in this adapter; tests repoint it.
_WSL_MOUNT_BASE = Path("/mnt")

_SNAPSHOT_CHUNK_BYTES = 1 << 20
_MAX_TIMESTAMP = datetime.max.replace(tzinfo=UTC)
# Roo Cline forked from Cline in late 2024; nothing earlier can be real.
_EARLIEST_PLAUSIBLE = datetime(2024, 1, 1, tzinfo=UTC)
_TIMESTAMP_FUTURE_SLACK = timedelta(days=1)

# Spec 6.4: markers that must never survive into extracted text.
_FORBIDDEN_TEXT_MARKERS = (
    "<environment_details>",
    "</environment_details>",
    "<task>",
    "</task>",
    "<feedback>",
    "</feedback>",
    "<answer>",
    "</answer>",
    "<user_message>",
    "</user_message>",
    '"status":',
)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _variant_editor_roots(base: Path) -> list[tuple[Path, Stability]]:
    roots: list[tuple[Path, Stability]] = [
        (base / name / "User" / "globalStorage", Stability.STABLE) for name in _STABLE_EDITOR_DIRS
    ]
    roots.extend(
        (base / name / "User" / "globalStorage", Stability.BETA) for name in _BETA_EDITOR_DIRS
    )
    return roots


def _code_server_root(home: Path) -> tuple[Path, Stability]:
    return (home / ".local" / "share" / "code-server" / "User" / "globalStorage", Stability.STABLE)


def _data_roots(context: DiscoveryContext) -> list[tuple[Path, Stability]]:
    """Known editor data roots for this platform (spec 1). Never guessed."""
    home = context.home
    environment = context.os_environment
    if environment is OsEnvironment.MACOS:
        return [
            *_variant_editor_roots(home / "Library" / "Application Support"),
            _code_server_root(home),
        ]
    if environment is OsEnvironment.WINDOWS:
        raw = context.environ.get("APPDATA", "").strip()
        base = Path(raw) if raw else home / "AppData" / "Roaming"
        return _variant_editor_roots(base)
    roots: list[tuple[Path, Stability]] = []
    if environment is OsEnvironment.WSL:
        roots.append(
            (home / ".vscode-server" / "data" / "User" / "globalStorage", Stability.STABLE)
        )
        roots.append(
            (
                home / ".vscode-server-insiders" / "data" / "User" / "globalStorage",
                Stability.STABLE,
            )
        )
    roots.extend(_variant_editor_roots(home / ".config"))
    roots.append(_code_server_root(home))
    return roots


def _windows_host_store_roots() -> list[Path]:
    """Windows-host Roo stores visible from WSL through DrvFS (spec 1).

    Fixed-layout existence checks only; nothing under the mount is opened.
    """
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
            roaming = profile / "AppData" / "Roaming"
            try:
                editors = sorted(roaming.iterdir()) if roaming.is_dir() else []
            except OSError:
                continue
            for editor in editors:
                for extension in EXTENSION_DIR_NAMES:
                    root = editor / "User" / "globalStorage" / extension
                    if root.is_dir():
                        found.append(root)
    return found


@dataclass(frozen=True)
class _Probe:
    """Aggregate discovery result for one instance root, before labeling."""

    root: Path
    path_hash: str
    stability: Stability
    accessibility: Accessibility
    diagnostic_code: str | None
    schema_fingerprint: str
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    candidate_messages: int
    candidate_words: int
    candidate_bytes: int


def _empty_probe(
    root: Path,
    stability: Stability,
    accessibility: Accessibility,
    diagnostic_code: str | None,
    fingerprint: str,
) -> _Probe:
    return _Probe(
        root=root,
        path_hash=_hash_text(_canonical_path(root)),
        stability=stability,
        accessibility=accessibility,
        diagnostic_code=diagnostic_code,
        schema_fingerprint=fingerprint,
        estimated_records=0,
        earliest=None,
        latest=None,
        candidate_messages=0,
        candidate_words=0,
        candidate_bytes=0,
    )


def _fingerprint(scans: list[TaskScan], tasks_dir: Path) -> str:
    """Spec 6.1 item 5: generations plus metadata and timestamp presence."""
    generations = sorted({scan.generation for scan in scans if scan.generation is not None})
    if not generations:
        return "empty"
    parts = [generations[0] if len(generations) == 1 else "mixed"]
    index_present = (tasks_dir / "_index.json").is_file()
    if index_present or any(scan.history_file_present for scan in scans):
        parts.append("meta")
    if any(scan.has_record_ts for scan in scans):
        parts.append("ts")
    return "+".join(parts)


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    tasks_scanned: int
    excluded_subtasks: int
    empty_tasks: int
    malformed_tasks: int
    unsupported_tasks: int
    oversized_tasks: int
    unbalanced_wrappers: int
    metadata_unreadable_tasks: int
    legacy_unmigrated_files: int
    ui_feedback_hashes: tuple[str, ...]
    extracted_text_hashes: frozenset[str]


class RooCodeAdapter:
    """SourceAdapter implementation for Roo Code per-task JSON stores."""

    def __init__(self, *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
        self._max_file_bytes = max_file_bytes
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
        return Stability.BETA

    def _capped(self, editor_stability: Stability) -> Stability:
        """No instance is more stable than the adapter that produced it.

        Editor variants set their own stability — VS Code proper is steadier
        than a fork — but that only ever lowers an instance. While the adapter
        itself is beta, every instance is beta at best.
        """
        order = (Stability.EXPERIMENTAL, Stability.BETA, Stability.STABLE)
        return min(editor_stability, self.stability, key=order.index)

    # -- file access guard --------------------------------------------------

    def _record_open(self, path: Path) -> None:
        """Assert the allowlist before every open; log for the verify audit."""
        if (
            path.name not in _ALLOWED_OPEN_NAMES
            or path.name in DENY_FILE_NAMES
            or any(part in DENY_DIR_NAMES for part in path.parts)
        ):
            msg = "refusing to open a path outside the Roo Code allowlist"
            raise PermissionError(msg)
        self._opened_paths.append(path)

    # -- discovery ------------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        probes = [
            self._probe_root(root, stability) for root, stability in self._instance_roots(context)
        ]
        if not probes:
            probes.append(
                _empty_probe(
                    self._default_root(context),
                    Stability.STABLE,
                    Accessibility.NOT_FOUND,
                    "SOURCE_NOT_FOUND",
                    "absent",
                )
            )
        if context.os_environment is OsEnvironment.WSL:
            # Spec 1: mounted Windows-host stores are hinted, never discovered.
            probes.extend(
                _empty_probe(
                    root,
                    Stability.BETA,
                    Accessibility.NOT_FOUND,
                    "SOURCE_WSL_HOST_STORE_HINT",
                    "windows-host",
                )
                for root in _windows_host_store_roots()
            )
        return self._label_and_build(context, probes)

    def _instance_roots(self, context: DiscoveryContext) -> list[tuple[Path, Stability]]:
        roots: list[tuple[Path, Stability]] = []
        seen: set[str] = set()
        for data_root, stability in _data_roots(context):
            for extension in EXTENSION_DIR_NAMES:
                root = data_root / extension
                try:
                    if not root.is_dir() or root.is_symlink():
                        continue
                except OSError:
                    continue
                key = _canonical_path(root)
                if key in seen:
                    continue
                seen.add(key)
                roots.append((root, stability))
        return roots

    def _default_root(self, context: DiscoveryContext) -> Path:
        return _data_roots(context)[0][0] / EXTENSION_DIR_NAMES[0]

    def _probe_root(self, root: Path, stability: Stability) -> _Probe:
        tasks_dir = root / "tasks"
        if not tasks_dir.is_dir():
            return _empty_probe(root, stability, Accessibility.FOUND, None, "empty")
        try:
            task_dirs = sorted(
                entry for entry in tasks_dir.iterdir() if entry.is_dir() and not entry.is_symlink()
            )
        except OSError:
            return _empty_probe(
                root, stability, Accessibility.INACCESSIBLE, "SOURCE_INACCESSIBLE", "unknown"
            )
        scans = self._scan_task_dirs(task_dirs)
        ok = [scan for scan in scans if scan.status is TaskStatus.OK]
        excluded = [scan for scan in scans if scan.status is TaskStatus.EXCLUDED_SUBTASK]
        unsupported = [scan for scan in scans if scan.status is TaskStatus.UNSUPPORTED]
        if unsupported and not ok and not excluded:
            # Spec 7: every recognizable task fails closed, so the instance does.
            return _empty_probe(
                root,
                stability,
                Accessibility.UNSUPPORTED_SCHEMA,
                "SOURCE_UNSUPPORTED_SCHEMA",
                "unsupported",
            )
        candidates = [candidate for scan in ok for candidate in scan.candidates]
        stamps = [
            candidate.timestamp for candidate in candidates if candidate.timestamp is not None
        ]
        return _Probe(
            root=root,
            path_hash=_hash_text(_canonical_path(root)),
            stability=stability,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=_fingerprint(scans, tasks_dir),
            estimated_records=sum(scan.parsed_records for scan in scans),
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            candidate_messages=len(candidates),
            candidate_words=sum(count_words(candidate.text) for candidate in candidates),
            candidate_bytes=sum(len(candidate.text.encode("utf-8")) for candidate in candidates),
        )

    def _scan_task_dirs(self, task_dirs: list[Path]) -> list[TaskScan]:
        scans: list[TaskScan] = []
        for directory in task_dirs:
            try:
                scans.append(
                    scan_task(
                        directory,
                        max_file_bytes=self._max_file_bytes,
                        record_open=self._record_open,
                    )
                )
            except OSError:
                # One unreadable task is skipped, never guessed about.
                continue
        apply_subtask_argument_match(scans)
        return scans

    def _label_and_build(self, context: DiscoveryContext, probes: list[_Probe]) -> DiscoveryOutcome:
        ordered = sorted(probes, key=lambda p: (p.earliest or _MAX_TIMESTAMP, p.path_hash))
        records: list[SourceInstanceRecord] = []
        instance_paths: dict[str, Path] = {}
        for position, probe in enumerate(ordered, start=1):
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
                    app_version=None,
                    stability=self._capped(probe.stability),
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

    # -- snapshot -------------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        target_dir.mkdir(parents=True, exist_ok=True)
        entries: list[SnapshotFileEntry] = []
        session_map: dict[str, str] = {}
        tasks_dir = source_path / "tasks"
        task_dirs: list[Path] = []
        if tasks_dir.is_dir():
            task_dirs = sorted(
                entry for entry in tasks_dir.iterdir() if entry.is_dir() and not entry.is_symlink()
            )
        for task_dir in task_dirs:
            session_hash = _hash_text(task_dir.name)
            subdir = session_hash[:16]
            copied_any = False
            for name in _SNAPSHOT_FILE_NAMES:
                source_file = task_dir / name
                if not source_file.is_file() or source_file.is_symlink():
                    continue
                if source_file.stat().st_size > self._max_file_bytes:
                    continue
                target_file = target_dir / subdir / name
                target_file.parent.mkdir(parents=True, exist_ok=True)
                digest, size = self._copy_validated(source_file, target_file)
                entries.append(
                    SnapshotFileEntry(
                        relative_path=f"{subdir}/{name}", size_bytes=size, sha256=digest
                    )
                )
                copied_any = True
            if copied_any:
                session_map[subdir] = session_hash
        payload = json.dumps({"session_hashes": session_map}, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        entries.append(
            SnapshotFileEntry(
                relative_path=_SNAPSHOT_META_NAME,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        return SnapshotCapture(snapshot_relative_dir=target_dir.name, files=entries)

    def _copy_validated(self, source: Path, target: Path) -> tuple[str, int]:
        """Copy bytes, then re-parse; retry once against an atomic-rewrite race."""
        digest, size = self._copy_bytes(source, target)
        if not self._parses_as_json(target):
            digest, size = self._copy_bytes(source, target)
            self._parses_as_json(target)
        return digest, size

    def _copy_bytes(self, source: Path, target: Path) -> tuple[str, int]:
        self._record_open(source)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(_SNAPSHOT_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
        return digest.hexdigest(), size

    def _parses_as_json(self, target: Path) -> bool:
        self._record_open(target)
        try:
            json.loads(target.read_bytes())
        except ValueError:
            return False
        return True

    # -- extraction -----------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        session_map = self._read_snapshot_meta(snapshot_dir)
        task_dirs = sorted(
            entry for entry in snapshot_dir.iterdir() if entry.is_dir() and not entry.is_symlink()
        )
        scans = self._scan_task_dirs(task_dirs)
        utterance_count = 0
        ui_hashes: list[str] = []
        text_hashes: set[str] = set()
        for scan in scans:
            if scan.status is not TaskStatus.OK:
                continue
            session_hash = session_map.get(scan.dir_name, _hash_text(scan.dir_name))
            # Metadata-less tasks follow the pre-3.50 rules (spec 4.3, 8.3).
            pre_metadata = not scan.has_history_item
            ui_hashes.extend(scan.ui_feedback_hashes)
            for candidate in scan.candidates:
                flags = list(candidate.flags)
                confidence = 0.9
                if candidate.is_initial and pre_metadata:
                    confidence = 0.6
                    flags.append("possible_delegated_task")
                text_hash = _hash_text(candidate.text)
                text_hashes.add(normalized_text_sha256(candidate.text))
                utterance_count += 1
                yield NormalizedUtterance(
                    utterance_id=(
                        f"{ADAPTER_ID}-{session_hash[:16]}-r{candidate.record_index:04d}"
                        f"b{candidate.block_index:02d}s{candidate.span_index:02d}"
                        f"-{text_hash[:12]}"
                    ),
                    source_adapter=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    session_hash=session_hash,
                    timestamp=candidate.timestamp,
                    text=candidate.text,
                    modality=Modality.WRITTEN,
                    text_status=TextStatus.CLEANED if candidate.cleaned else TextStatus.VERBATIM,
                    authorship_confidence=confidence,
                    authorship_basis=f"explicit_user_role+{candidate.carrier}",
                    source_path_hash=instance.path_hash,
                    content_flags=flags,
                )
        self._extraction_stats[instance.instance_key] = ExtractionStats(
            utterance_count=utterance_count,
            tasks_scanned=len(scans),
            excluded_subtasks=sum(
                1 for scan in scans if scan.status is TaskStatus.EXCLUDED_SUBTASK
            ),
            empty_tasks=sum(1 for scan in scans if scan.status is TaskStatus.EMPTY),
            malformed_tasks=sum(1 for scan in scans if scan.status is TaskStatus.MALFORMED),
            unsupported_tasks=sum(1 for scan in scans if scan.status is TaskStatus.UNSUPPORTED),
            oversized_tasks=sum(1 for scan in scans if scan.status is TaskStatus.OVERSIZED),
            unbalanced_wrappers=sum(scan.unbalanced_wrappers for scan in scans),
            metadata_unreadable_tasks=sum(1 for scan in scans if scan.metadata_unreadable),
            legacy_unmigrated_files=sum(1 for scan in scans if scan.legacy_unmigrated),
            ui_feedback_hashes=tuple(ui_hashes),
            extracted_text_hashes=frozenset(text_hashes),
        )

    def _read_snapshot_meta(self, snapshot_dir: Path) -> dict[str, str]:
        meta_path = snapshot_dir / _SNAPSHOT_META_NAME
        if not meta_path.is_file():
            return {}
        self._record_open(meta_path)
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        hashes = payload.get("session_hashes")
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

    # -- verification ---------------------------------------------------------

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
                        "utterance does not belong to the roo_code adapter",
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
            if any(marker in utterance.text for marker in _FORBIDDEN_TEXT_MARKERS):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text contains a wrapper, environment, or status artifact",
                        item_ref=utterance.utterance_id,
                    )
                )
            if utterance.timestamp is not None and not (
                _EARLIEST_PLAUSIBLE <= utterance.timestamp <= ceiling
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
        stats = self._extraction_stats.get(instance.instance_key)
        if stats is not None:
            for ui_hash in stats.ui_feedback_hashes:
                if ui_hash not in stats.extracted_text_hashes:
                    # Spec 6.4 cross-check; the API history stays authoritative.
                    diagnostics.append(
                        Diagnostic.from_code(
                            "CARDINALITY_MISMATCH",
                            "ui_api_mismatch: a UI user_feedback entry has no matching "
                            "extracted utterance (API history is authoritative)",
                            item_ref=instance.instance_key,
                        )
                    )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory registered by the adapter coordinator."""
    return RooCodeAdapter()
