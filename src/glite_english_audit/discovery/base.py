"""The source-adapter protocol every adapter implements.

Adapters are version-aware and read-only toward source applications
(specification, 4.1). Discovery parses source contents locally to produce
aggregate inventory records; it never returns source text. Extraction runs
only against a consistent snapshot, never against live application data.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from glite_english_audit.artifacts.enums import OsEnvironment, Stability
from glite_english_audit.artifacts.models import (
    NormalizedUtterance,
    SnapshotFileEntry,
    SourceInstanceRecord,
)
from glite_english_audit.diagnostics.codes import Diagnostic


@dataclass(frozen=True)
class DiscoveryContext:
    """Everything discovery may look at, injectable for tests."""

    os_environment: OsEnvironment
    home: Path
    now: datetime
    environ: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryOutcome:
    """Discovery result: private records plus the local key-to-path map.

    ``instance_paths`` stays in the private runtime store so the run manifest
    can resolve an opaque label locally. It is never shown to the model.
    """

    records: list[SourceInstanceRecord]
    instance_paths: dict[str, Path]


@dataclass(frozen=True)
class SnapshotCapture:
    """Files an adapter copied into a snapshot directory.

    The orchestration wraps this into a :class:`SnapshotManifest` with an
    envelope; the file list doubles as the manifest-bounded cleanup plan.
    """

    snapshot_relative_dir: str
    files: list[SnapshotFileEntry]


class SourceAdapter(Protocol):
    """Contract for one local source application adapter."""

    @property
    def adapter_id(self) -> str:
        """Stable public adapter ID, such as ``claude_code``."""
        ...

    @property
    def adapter_version(self) -> str:
        """Adapter implementation version recorded in every artifact."""
        ...

    @property
    def stability(self) -> Stability:
        """Release maturity used by default source selection."""
        ...

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        """Detect local instances and compute aggregate inventory, locally."""
        ...

    def snapshot(
        self,
        instance: SourceInstanceRecord,
        source_path: Path,
        target_dir: Path,
    ) -> SnapshotCapture:
        """Copy a consistent, read-only snapshot of one instance."""
        ...

    def extract(
        self,
        instance: SourceInstanceRecord,
        snapshot_dir: Path,
    ) -> Iterator[NormalizedUtterance]:
        """Yield candidate user-authored utterances from a snapshot."""
        ...

    def verify(
        self,
        instance: SourceInstanceRecord,
        utterances: list[NormalizedUtterance],
    ) -> list[Diagnostic]:
        """Adapter-specific structural and semantic checks on extraction."""
        ...
