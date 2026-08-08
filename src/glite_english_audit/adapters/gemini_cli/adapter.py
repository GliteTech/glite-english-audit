"""The Google Gemini CLI source adapter.

Discovers per-project session instances under ``<root>/tmp/<project-id>/chats/``
where the root is ``$GEMINI_CLI_HOME/.gemini`` when set, else ``~/.gemini``,
snapshots the plain JSON/JSONL session files byte-for-byte, and extracts
candidate user-authored utterances per ``specifications/sources/gemini_cli.md``.

The adapter opens only top-level ``chats/*.json`` and ``chats/*.jsonl`` files.
The allowlist is enforced before every open; the never-open denylist check is
defense in depth (spec section 2.4). Subdirectories of ``chats/`` are subagent
session trees and are never opened or descended into. Discovery never returns,
prints, or logs source text.
"""

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.adapters.gemini_cli.records import (
    KeptMessage,
    SessionScan,
    scan_chat_file,
    text_starts_with_injected_marker,
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

ADAPTER_ID = "gemini_cli"
ADAPTER_VERSION = "1.0.0"
PRODUCER_VERSION = ADAPTER_VERSION

_HUMAN_NAME = "Gemini CLI"
_STORAGE_FORMAT = "json/jsonl"
_HOME_ENV = "GEMINI_CLI_HOME"
_SNAPSHOT_META_NAME = "gemini-cli-source-paths.json"

# Spec 2.4: never-open denylist. The chats/-only allowlist is the primary
# barrier; these names are re-checked before every open as defense in depth.
DENY_FILE_NAMES: frozenset[str] = frozenset(
    {
        "oauth_creds.json",
        "google_accounts.json",
        "mcp-oauth-tokens.json",
        "a2a-oauth-tokens.json",
        "installation_id",
        "settings.json",
        "keybindings.json",
        "trustedFolders.json",
        "projects.json",
        "GEMINI.md",
        "logs.json",
        "shell_history",
    }
)
DENY_DIR_NAMES: frozenset[str] = frozenset(
    {
        "commands",
        "skills",
        "agents",
        "policies",
        "extensions",
        "history",
        "checkpoints",
        "plans",
        "tasks",
        "memory",
        "otel",
    }
)

# Spec 2.4/3.4: /chat save artifacts under tmp/<id>/ are detected by name for
# the fingerprint but never opened.
_CHECKPOINT_NAME = re.compile(r"^checkpoint-.*\.json$")

_CHAT_SUFFIXES = frozenset({".json", ".jsonl"})
_MAX_TIMESTAMP = datetime.max.replace(tzinfo=UTC)
_EARLIEST_PLAUSIBLE = datetime(2020, 1, 1, tzinfo=UTC)

# Spec section 1: fixed DrvFS mount prefix for the WSL host-store probe.
# Tests patch this constant; production never walks profile directories.
_WSL_MOUNT_ROOT = Path("/mnt")
_WINDOWS_PROFILE = re.compile(r"^([A-Za-z]):[\\/](.+)$")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _select_canonical(scans: Sequence[SessionScan]) -> tuple[list[SessionScan], tuple[str, ...]]:
    """Spec 6.3: files sharing one session identity collapse; .jsonl canonical."""
    groups: dict[str, list[SessionScan]] = {}
    keys: list[str] = []
    passthrough: list[SessionScan] = []
    for scan in scans:
        identity = scan.session_identity
        if identity is None:
            passthrough.append(scan)
            continue
        if identity not in groups:
            groups[identity] = []
            keys.append(identity)
        groups[identity].append(scan)
    canonical: list[SessionScan] = []
    dropped: list[str] = []
    for key in keys:
        members = sorted(
            groups[key],
            key=lambda scan: (0 if scan.file_name.endswith(".jsonl") else 1, scan.file_name),
        )
        canonical.append(members[0])
        dropped.extend(member.file_name for member in members[1:])
    return [*canonical, *passthrough], tuple(dropped)


@dataclass(frozen=True)
class CandidateSet:
    """Deduplicated candidate messages plus the canonical scans behind them."""

    pairs: list[tuple[SessionScan, KeptMessage]]
    canonical: list[SessionScan]
    dropped_files: tuple[str, ...]


def _candidate_set(scans: Sequence[SessionScan]) -> CandidateSet:
    supported = [scan for scan in scans if scan.extractable]
    canonical, dropped = _select_canonical(supported)
    pairs = [(scan, message) for scan in canonical if not scan.subagent for message in scan.kept]
    return CandidateSet(pairs=pairs, canonical=canonical, dropped_files=dropped)


def _fingerprint(scans: Sequence[SessionScan], *, subagent_dirs: bool, checkpoints: bool) -> str:
    """Spec 3.3 storage-variant fingerprint: generations plus feature presence."""
    supported = [scan for scan in scans if scan.extractable]
    generations = {scan.generation for scan in supported if scan.generation is not None}
    if not generations:
        base = "empty"
    elif len(generations) > 1:
        base = "mixed"
    else:
        base = next(iter(generations))
    parts = [base]
    if any(scan.has_display_content for scan in supported):
        parts.append("display")
    if any(scan.has_notice_types for scan in supported):
        parts.append("notices")
    if any(scan.has_kind_metadata for scan in supported):
        parts.append("kind")
    if subagent_dirs:
        parts.append("subagent-dirs")
    if checkpoints:
        parts.append("checkpoints")
    return "+".join(parts)


@dataclass(frozen=True)
class ExtractionStats:
    """Per-instance extraction accounting kept for verification."""

    utterance_count: int
    files_scanned: int
    unsupported_files: tuple[str, ...]
    malformed_files: tuple[str, ...]
    oversize_files: tuple[str, ...]
    dropped_duplicate_files: tuple[str, ...]
    truncated_tail_files: tuple[str, ...]
    malformed_lines: int
    skipped_unknown_lines: int


@dataclass
class _ProvisionalInstance:
    """One project directory's aggregates before label assignment."""

    root: Path
    path_hash: str
    accessibility: Accessibility
    diagnostic_code: str | None
    schema_fingerprint: str
    stability: Stability
    estimated_records: int
    earliest: datetime | None
    latest: datetime | None
    messages: int
    words: int
    bytes_count: int


def _degenerate_instance(
    root: Path,
    *,
    accessibility: Accessibility,
    diagnostic_code: str | None,
    fingerprint: str,
    stability: Stability = Stability.STABLE,
) -> _ProvisionalInstance:
    return _ProvisionalInstance(
        root=root,
        path_hash=_hash_text(_canonical_path(root)),
        accessibility=accessibility,
        diagnostic_code=diagnostic_code,
        schema_fingerprint=fingerprint,
        stability=stability,
        estimated_records=0,
        earliest=None,
        latest=None,
        messages=0,
        words=0,
        bytes_count=0,
    )


class GeminiCliAdapter:
    """SourceAdapter implementation for the Gemini CLI session store."""

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
        return Stability.BETA

    # -- discovery ---------------------------------------------------------

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        root = self._storage_root(context)
        provisional: list[_ProvisionalInstance] = []
        if root.is_dir():
            tmp_dir = root / "tmp"
            if tmp_dir.is_dir():
                try:
                    provisional = [
                        self._scan_instance(directory) for directory in self._instance_dirs(tmp_dir)
                    ]
                except OSError:
                    return self._single_record_outcome(
                        context,
                        root,
                        accessibility=Accessibility.INACCESSIBLE,
                        diagnostic_code="SOURCE_INACCESSIBLE",
                        fingerprint="unknown",
                    )
        host = self._wsl_host_instance(context)
        if host is not None:
            provisional.append(host)
        if provisional:
            return self._label_and_build(context, provisional)
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

    def _storage_root(self, context: DiscoveryContext) -> Path:
        """Spec 2.1: honor GEMINI_CLI_HOME when its .gemini exists; never create."""
        override = context.environ.get(_HOME_ENV, "").strip()
        if override:
            candidate = Path(override) / ".gemini"
            if candidate.is_dir():
                return candidate
        return context.home / ".gemini"

    def _instance_dirs(self, tmp_dir: Path) -> list[Path]:
        """Spec 2.2: every tmp/<dir> containing a chats/ directory is an instance."""
        result: list[Path] = []
        for entry in sorted(tmp_dir.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            chats = entry / "chats"
            if chats.is_dir() and not chats.is_symlink():
                result.append(entry)
        return result

    def _wsl_host_instance(self, context: DiscoveryContext) -> _ProvisionalInstance | None:
        """Spec sections 1 and 10.7: report a Windows-host store seen from WSL.

        The probe checks only the fixed ``<profile>/.gemini/tmp`` layout derived
        from the WSL-visible Windows profile; it never walks mounted profile
        directories. The instance ships experimental and contributes no
        analyzable text until the dual-setup smoke test passes.
        """
        if context.os_environment is not OsEnvironment.WSL:
            return None
        match = _WINDOWS_PROFILE.match(context.environ.get("USERPROFILE", "").strip())
        if match is None:
            return None
        drive = match.group(1).lower()
        profile_tail = match.group(2).replace("\\", "/").strip("/")
        host_root = _WSL_MOUNT_ROOT / drive / profile_tail / ".gemini"
        if not (host_root / "tmp").is_dir():
            return None
        return _degenerate_instance(
            host_root,
            accessibility=Accessibility.FOUND,
            diagnostic_code="SOURCE_WSL_HOST_STORE_HINT",
            fingerprint="wsl-host-untested",
            stability=Stability.EXPERIMENTAL,
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
        provisional = _degenerate_instance(
            root,
            accessibility=accessibility,
            diagnostic_code=diagnostic_code,
            fingerprint=fingerprint,
        )
        return self._label_and_build(context, [provisional])

    def _scan_instance(self, directory: Path) -> _ProvisionalInstance:
        path_hash = _hash_text(_canonical_path(directory))
        chats = directory / "chats"
        try:
            scans = [self._scan_file(path) for path in self._chat_files(chats)]
            subagent_dirs = any(entry.is_dir() for entry in chats.iterdir())
            checkpoints = self._checkpoint_names_present(directory)
        except OSError:
            return _degenerate_instance(
                directory,
                accessibility=Accessibility.INACCESSIBLE,
                diagnostic_code="SOURCE_INACCESSIBLE",
                fingerprint="unknown",
            )
        if scans and all(scan.unsupported for scan in scans):
            return _degenerate_instance(
                directory,
                accessibility=Accessibility.UNSUPPORTED_SCHEMA,
                diagnostic_code="SOURCE_UNSUPPORTED_SCHEMA",
                fingerprint="unsupported",
            )
        candidates = _candidate_set(scans)
        stamps = [
            stamp
            for scan, message in candidates.pairs
            if (stamp := message.timestamp or scan.start_time) is not None
        ]
        return _ProvisionalInstance(
            root=directory,
            path_hash=path_hash,
            accessibility=Accessibility.FOUND,
            diagnostic_code=None,
            schema_fingerprint=_fingerprint(
                scans, subagent_dirs=subagent_dirs, checkpoints=checkpoints
            ),
            stability=self.stability,
            estimated_records=sum(scan.message_records for scan in candidates.canonical),
            earliest=min(stamps) if stamps else None,
            latest=max(stamps) if stamps else None,
            messages=len(candidates.pairs),
            words=sum(count_words(message.text) for _, message in candidates.pairs),
            bytes_count=sum(len(message.text.encode("utf-8")) for _, message in candidates.pairs),
        )

    @staticmethod
    def _checkpoint_names_present(directory: Path) -> bool:
        """Names-only detection (spec 3.4); checkpoint files are never opened."""
        for entry in directory.iterdir():
            if entry.name == "checkpoints" and entry.is_dir():
                return True
            if entry.is_file() and _CHECKPOINT_NAME.match(entry.name):
                return True
        return False

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
                    stability=item.stability,
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

    def _chat_files(self, chats_dir: Path) -> list[Path]:
        """Spec 2.3 allowlist: top-level chats/*.json[l] only; no subdirectories."""
        if not chats_dir.is_dir():
            return []
        return sorted(
            entry
            for entry in chats_dir.iterdir()
            if entry.is_file()
            and not entry.is_symlink()
            and entry.suffix in _CHAT_SUFFIXES
            and entry.name not in DENY_FILE_NAMES
        )

    def _assert_allowlisted_chat_file(self, path: Path) -> None:
        if (
            path.suffix not in _CHAT_SUFFIXES
            or path.parent.name != "chats"
            or path.name in DENY_FILE_NAMES
            or path.parent.parent.name in DENY_DIR_NAMES
        ):
            msg = "refusing to open a path outside the chat-file allowlist"
            raise PermissionError(msg)

    def _scan_file(self, path: Path) -> SessionScan:
        self._assert_allowlisted_chat_file(path)
        self._opened_paths.append(path)
        return scan_chat_file(path)

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o700)
        chats_target = target_dir / "chats"
        chats_target.mkdir(exist_ok=True)
        chats_target.chmod(0o700)
        entries: list[SnapshotFileEntry] = []
        path_hashes: dict[str, str] = {}
        for source_file in self._chat_files(source_path / "chats"):
            self._assert_allowlisted_chat_file(source_file)
            self._opened_paths.append(source_file)
            target_file = chats_target / source_file.name
            digest, size = self._copy_bytes(source_file, target_file)
            target_file.chmod(0o600)
            relative = f"chats/{source_file.name}"
            entries.append(
                SnapshotFileEntry(relative_path=relative, size_bytes=size, sha256=digest)
            )
            path_hashes[relative] = _hash_text(_canonical_path(source_file))
        payload = json.dumps({"source_path_hashes": path_hashes}, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        meta_path = target_dir / _SNAPSHOT_META_NAME
        meta_path.write_bytes(payload)
        meta_path.chmod(0o600)
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
        return digest.hexdigest(), size

    # -- extraction --------------------------------------------------------

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        path_hashes = self._read_snapshot_meta(snapshot_dir)
        scans = [self._scan_file(path) for path in self._chat_files(snapshot_dir / "chats")]
        candidates = _candidate_set(scans)
        for scan, message in candidates.pairs:
            identity = scan.session_identity
            if identity is None:
                continue
            session_hash = _hash_text(identity)
            flags = set(message.content_flags)
            if scan.session_meta_missing:
                flags.add("session_meta_missing")
            yield NormalizedUtterance(
                utterance_id=f"{ADAPTER_ID}-{session_hash[:16]}-{message.message_id}",
                source_adapter=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                session_hash=session_hash,
                timestamp=message.timestamp or scan.start_time,
                text=message.text,
                modality=Modality.WRITTEN,
                text_status=TextStatus.VERBATIM,
                authorship_confidence=0.95 if message.display_used else 0.9,
                authorship_basis=(
                    "explicit_user_role+display_content_preferred"
                    if message.display_used
                    else "explicit_user_role"
                ),
                source_path_hash=path_hashes.get(f"chats/{scan.file_name}", instance.path_hash),
                content_flags=sorted(flags),
            )
        self._extraction_stats[instance.instance_key] = ExtractionStats(
            utterance_count=len(candidates.pairs),
            files_scanned=len(scans),
            unsupported_files=tuple(scan.file_name for scan in scans if scan.unsupported),
            malformed_files=tuple(scan.file_name for scan in scans if scan.malformed_file),
            oversize_files=tuple(scan.file_name for scan in scans if scan.oversize_skipped),
            dropped_duplicate_files=candidates.dropped_files,
            truncated_tail_files=tuple(scan.file_name for scan in scans if scan.truncated_tail),
            malformed_lines=sum(scan.malformed_lines for scan in scans),
            skipped_unknown_lines=sum(scan.skipped_unknown_lines for scan in scans),
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
                        "duplicate utterance ID after session deduplication",
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
                        "utterance does not belong to the gemini_cli adapter",
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
            elif text_starts_with_injected_marker(utterance.text):
                diagnostics.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "utterance text begins with an injected wrapper or command marker",
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
            if path.name == _SNAPSHOT_META_NAME:
                continue
            if path.name in DENY_FILE_NAMES or path.parent.name != "chats":
                diagnostics.append(
                    Diagnostic.from_code(
                        "SOURCE_SNAPSHOT_UNSAFE_PATH",
                        "a non-allowlisted path appears in the opened-path audit log",
                    )
                )
        return diagnostics


def create_adapter() -> SourceAdapter:
    """Factory registered by the adapter coordinator."""
    return GeminiCliAdapter()
