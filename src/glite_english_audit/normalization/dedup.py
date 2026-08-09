"""Cross-source deduplication of normalized utterances (specification, 4.8).

Removes copies of the same production event that appear in more than one
source — most commonly text dictated in Wispr Flow and pasted into an agent
seconds later — while preserving genuinely repeated language produced on
separate occasions.

A pair of utterances may collapse only when it crosses sources: different
adapters, or the same adapter with a different instance
(``source_path_hash``). Repetition inside one instance is that source's own
history, never a copy.

- Exact duplicates share the same normalized text (casefold, collapsed
  whitespace). With both timestamps present they collapse only within
  ``TEMPORAL_PROXIMITY_LIMIT``; identical text far apart in time is repeated
  language and stays. With a missing timestamp, exact duplicates still
  collapse — the text is identical, so the copy is unambiguous.
- Fuzzy duplicates require both timestamps within
  ``TEMPORAL_PROXIMITY_LIMIT`` *and* ``difflib.SequenceMatcher`` ratio (on
  normalized text, ``autojunk=False`` for determinism) of at least
  ``FUZZY_RATIO_THRESHOLD``. Without both timestamps, only exact matches
  dedupe — similarity alone never proves the same event.

Each duplicate cluster keeps one canonical utterance: ``verbatim`` text
status first, then ``spoken_asr`` modality (the original production event
for dictated-then-pasted text), then earliest timestamp, then lowest
utterance ID. Excluded copies are recorded locally; matching provenance is
never exported (specification, 4.8).

Fuzzy comparison is guarded against O(n^2) blowup: candidates are sorted by
timestamp and compared in a sliding window that stops at the 10-minute
proximity limit — far tighter than the 48-hour bucket bound the
specification requires.
"""

import difflib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.enums import Modality, TextStatus
from glite_english_audit.artifacts.envelope import as_utc
from glite_english_audit.artifacts.models import NormalizedUtterance

PRODUCER_VERSION = "1.0.0"

# Minimum SequenceMatcher ratio for a fuzzy duplicate; the boundary is
# inclusive (ratio == 0.92 collapses).
FUZZY_RATIO_THRESHOLD = 0.92

# Maximum timestamp distance for the same production event; the boundary is
# inclusive (exactly 10 minutes apart collapses).
TEMPORAL_PROXIMITY_LIMIT = timedelta(minutes=10)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

DedupKind = Literal["exact", "fuzzy"]


class DedupExclusion(BaseModel):
    """Local-only record of one removed duplicate copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    excluded_utterance_id: str
    canonical_utterance_id: str
    kind: DedupKind


class DedupOutcome(BaseModel):
    """Canonical utterances plus the excluded copies, both in stable order."""

    model_config = ConfigDict(extra="forbid")

    canonical: list[NormalizedUtterance]
    excluded: list[DedupExclusion] = Field(default_factory=list)


def _normalized_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _stamp(utterance: NormalizedUtterance) -> datetime | None:
    """The utterance's timestamp on the one comparable scale.

    Sources that record timezone-unknown local time reach this step naive, so
    every subtraction and ordering below would raise against an aware
    timestamp from a sibling source.
    """
    return None if utterance.timestamp is None else as_utc(utterance.timestamp)


def _order_key(utterance: NormalizedUtterance) -> tuple[int, datetime, str]:
    """Timestamped utterances first by time, then untimestamped by ID."""
    stamp = _stamp(utterance)
    if stamp is None:
        return (1, _EPOCH, utterance.utterance_id)
    return (0, stamp, utterance.utterance_id)


def _canonical_key(utterance: NormalizedUtterance) -> tuple[int, int, int, datetime, str]:
    stamp = _stamp(utterance)
    return (
        0 if utterance.text_status is TextStatus.VERBATIM else 1,
        0 if utterance.modality is Modality.SPOKEN_ASR else 1,
        0 if stamp is not None else 1,
        stamp if stamp is not None else _EPOCH,
        utterance.utterance_id,
    )


def _cross_source(a: NormalizedUtterance, b: NormalizedUtterance) -> bool:
    if a.source_adapter != b.source_adapter:
        return True
    return a.source_path_hash != b.source_path_hash


def dedupe(utterances: list[NormalizedUtterance]) -> DedupOutcome:
    """Collapse cross-source copies of the same production event.

    The result does not depend on input order: utterances are sorted into a
    deterministic order first, and both output lists follow it (timestamp,
    then utterance ID; utterances without timestamps last).
    """
    ordered = sorted(utterances, key=_order_key)
    count = len(ordered)
    parent = list(range(count))

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            parent[index], index = root, parent[index]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    keys = [_normalized_key(u.text) for u in ordered]

    # Exact pass: group by normalized text.
    by_key: dict[str, list[int]] = {}
    for index, key in enumerate(keys):
        by_key.setdefault(key, []).append(index)
    for indices in by_key.values():
        if len(indices) < 2:
            continue
        timed = [(t, i) for i in indices if (t := _stamp(ordered[i])) is not None]
        untimed = [i for i in indices if ordered[i].timestamp is None]
        for position, (ts_left, left) in enumerate(timed):
            for ts_right, right in timed[position + 1 :]:
                if ts_right - ts_left > TEMPORAL_PROXIMITY_LIMIT:
                    break
                if _cross_source(ordered[left], ordered[right]):
                    union(left, right)
        # A missing timestamp cannot disprove proximity; identical text
        # across sources is still an unambiguous copy.
        for left in untimed:
            for right in indices:
                if left != right and _cross_source(ordered[left], ordered[right]):
                    union(left, right)

    # Fuzzy pass: timestamped utterances only, sliding proximity window.
    timed_all = [(t, i) for i, u in enumerate(ordered) if (t := _stamp(u)) is not None]
    for position, (ts_left, left) in enumerate(timed_all):
        for ts_right, right in timed_all[position + 1 :]:
            if ts_right - ts_left > TEMPORAL_PROXIMITY_LIMIT:
                break
            if keys[left] == keys[right] or find(left) == find(right):
                continue
            if not _cross_source(ordered[left], ordered[right]):
                continue
            matcher = difflib.SequenceMatcher(a=keys[left], b=keys[right], autojunk=False)
            if matcher.real_quick_ratio() < FUZZY_RATIO_THRESHOLD:
                continue
            if matcher.quick_ratio() < FUZZY_RATIO_THRESHOLD:
                continue
            if matcher.ratio() >= FUZZY_RATIO_THRESHOLD:
                union(left, right)

    clusters: dict[int, list[int]] = {}
    for index in range(count):
        clusters.setdefault(find(index), []).append(index)

    canonical_indices: list[int] = []
    exclusion_pairs: list[tuple[int, DedupExclusion]] = []
    for members in clusters.values():
        canonical = min(members, key=lambda m: _canonical_key(ordered[m]))
        canonical_indices.append(canonical)
        for member in members:
            if member == canonical:
                continue
            kind: DedupKind = "exact" if keys[member] == keys[canonical] else "fuzzy"
            exclusion_pairs.append(
                (
                    member,
                    DedupExclusion(
                        excluded_utterance_id=ordered[member].utterance_id,
                        canonical_utterance_id=ordered[canonical].utterance_id,
                        kind=kind,
                    ),
                )
            )

    exclusion_pairs.sort(key=lambda pair: pair[0])
    return DedupOutcome(
        canonical=[ordered[i] for i in sorted(canonical_indices)],
        excluded=[exclusion for _, exclusion in exclusion_pairs],
    )
