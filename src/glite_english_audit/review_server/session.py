"""Mutable review state behind the loopback review page.

One session wraps the privacy-approved reviewed artifact. Every record starts
included; the user can only include or exclude records, never edit them
(specification, 2.6). Counts are recomputed on every toggle so the arithmetic
identity from Section 5.6 holds at all times, and the exported package is
regenerated lazily after any change so a stale payload can never be sent.
"""

import threading

from glite_english_audit.artifacts.hashing import canonical_json_bytes
from glite_english_audit.artifacts.models import (
    AuditCounts,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
)
from glite_english_audit.artifacts.submission import SubmissionPackage
from glite_english_audit.submission.package import MaterializationError, materialize_package


class UnknownMistakeError(Exception):
    """A toggle referenced a mistake ID that is not part of this session."""

    def __init__(self, mistake_id: str) -> None:
        super().__init__(f"unknown mistake_id: {mistake_id!r}")
        self.mistake_id = mistake_id


class ReviewSessionState:
    """Thread-safe working state for one final-review session."""

    def __init__(self, reviewed: ReviewedSubmissionArtifact) -> None:
        self._lock = threading.RLock()
        self._envelope = reviewed.envelope
        self._base_counts = reviewed.counts
        self._records: dict[str, ReviewedRecord] = {}
        for record in reviewed.records:
            if record.mistake_id in self._records:
                msg = f"duplicate mistake_id in reviewed artifact: {record.mistake_id!r}"
                raise ValueError(msg)
            self._records[record.mistake_id] = record.model_copy(update={"included": True})
        self._adult_confirmed = False
        self._storage_confirmed = False
        self._package: SubmissionPackage | None = None
        self._package_bytes: bytes | None = None

    @property
    def records(self) -> list[ReviewedRecord]:
        """The working records, in input order, with current include flags."""
        with self._lock:
            return list(self._records.values())

    @property
    def adult_confirmed(self) -> bool:
        with self._lock:
            return self._adult_confirmed

    @property
    def storage_confirmed(self) -> bool:
        with self._lock:
            return self._storage_confirmed

    @property
    def included_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._records.values() if record.included)

    @property
    def excluded_count(self) -> int:
        with self._lock:
            return len(self._records) - self.included_count

    @property
    def counts(self) -> AuditCounts:
        """The current count set, recomputed from the working include flags."""
        with self._lock:
            return self._recount()

    def set_adult_confirmed(self, value: bool) -> None:
        with self._lock:
            self._adult_confirmed = value

    def set_storage_confirmed(self, value: bool) -> None:
        with self._lock:
            self._storage_confirmed = value

    def set_included(self, mistake_id: str, included: bool) -> AuditCounts:
        """Include or exclude one record and return the updated counts.

        A real change invalidates the cached package, so the next package
        request materializes a fresh one with a new payload identity.
        """
        with self._lock:
            record = self._records.get(mistake_id)
            if record is None:
                raise UnknownMistakeError(mistake_id)
            if record.included != included:
                self._records[mistake_id] = record.model_copy(update={"included": included})
                self._package = None
                self._package_bytes = None
            return self._recount()

    def current_reviewed_artifact(self) -> ReviewedSubmissionArtifact:
        """The reviewed artifact reflecting the current include decisions."""
        with self._lock:
            return ReviewedSubmissionArtifact(
                envelope=self._envelope,
                records=list(self._records.values()),
                counts=self._recount(),
            )

    def current_package(self) -> SubmissionPackage | None:
        """The package for the current selection, or None when nothing can be sent.

        The package is cached until a toggle changes the selection. None means
        either zero included records or a failed deterministic gate; in both
        cases there is nothing safe to send or download.
        """
        with self._lock:
            if self._package is not None:
                return self._package
            if self.included_count == 0:
                return None
            try:
                package = materialize_package(self.current_reviewed_artifact())
            except MaterializationError:
                return None
            self._package = package
            self._package_bytes = canonical_json_bytes(package.model_dump(mode="json"))
            return package

    def current_package_bytes(self) -> bytes | None:
        """Canonical JSON bytes of the current package: shown, downloaded, sent."""
        with self._lock:
            if self._package_bytes is None:
                self.current_package()
            return self._package_bytes

    def _recount(self) -> AuditCounts:
        included = sum(1 for record in self._records.values() if record.included)
        excluded = len(self._records) - included
        payload = self._base_counts.model_dump(mode="python")
        payload["shared_mistakes"] = included
        payload["withheld_by_user"] = excluded
        return AuditCounts.model_validate(payload)
