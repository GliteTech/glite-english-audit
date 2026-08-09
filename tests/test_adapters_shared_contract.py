"""Invariants every adapter shares, checked across all nine at once.

The nine adapters were written separately, so the rules that must hold for
all of them are the ones most likely to drift. Each test here fails on the
adapter that broke ranks rather than on the pipeline stage that noticed.
"""

import os
import shutil
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit import adapters
from glite_english_audit.artifacts.enums import (
    Accessibility,
    Modality,
    OsEnvironment,
    Stability,
    TextStatus,
)
from glite_english_audit.artifacts.models import PUBLIC_SOURCE_TYPES, NormalizedUtterance
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.registry import adapter_ids, create_adapter
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.paths import repo_root
from glite_english_audit.pipeline.collect import _within_bounds

# Increasing maturity. Comparing enum members directly would compare their
# string values, which sort beta < experimental < stable.
_MATURITY = (Stability.EXPERIMENTAL, Stability.BETA, Stability.STABLE)


@pytest.fixture(autouse=True)
def _registered() -> None:
    adapters.register_all()


def _context(home: Path) -> DiscoveryContext:
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ={},
    )


def test_every_shipped_adapter_id_is_a_submittable_source_type() -> None:
    """A record naming a registered adapter must survive submission validation.

    ``PUBLIC_SOURCE_TYPES`` is frozen rather than derived, so a new adapter
    can register without becoming submittable. That gap is silent until a
    real run reaches stage 6.
    """
    assert set(adapter_ids()) <= PUBLIC_SOURCE_TYPES


@pytest.mark.parametrize("adapter_id", sorted(PUBLIC_SOURCE_TYPES))
def test_no_instance_claims_more_maturity_than_its_adapter(adapter_id: str, tmp_path: Path) -> None:
    """Beta and experimental adapters must not emit a ``stable`` instance.

    Default selection reads the instance, not the adapter, so an instance
    that overstates its maturity is selected by default from an adapter that
    the release gates say must be opted into.
    """
    adapter = create_adapter(adapter_id)
    ceiling = _MATURITY.index(adapter.stability)
    home = tmp_path / adapter_id
    # A bare home exercises the not-found path; the store roots below exercise
    # the "directory exists but holds nothing" path, which is where a
    # degenerate record is built from defaults rather than from a scan.
    for relative in (".gemini", ".codex", ".claude", ".cline", ".cursor", ".aider"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    for record in adapter.discover(_context(home)).records:
        assert _MATURITY.index(record.stability) <= ceiling, (
            f"{adapter_id} emitted a {record.stability.value} instance "
            f"from a {adapter.stability.value} adapter"
        )


def _prepared_home(adapter_id: str, destination: Path) -> Path:
    """A writable copy of one adapter's success fixture, ready to discover.

    SQLite-backed fixtures ship as ``*.sql`` text so the committed tree stays
    readable and diffable; the database only exists once it is replayed.
    """
    home = destination / "home"
    shutil.copytree(repo_root() / "fixtures" / adapter_id / "success" / "home", home)
    for sql_path in sorted(home.rglob("*.sql")):
        connection = sqlite3.connect(sql_path.with_suffix(""))
        try:
            connection.executescript(sql_path.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()
        sql_path.unlink()
    return home


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes only")
@pytest.mark.parametrize("adapter_id", sorted(PUBLIC_SOURCE_TYPES))
def test_snapshot_copies_are_owner_only(adapter_id: str, tmp_path: Path) -> None:
    """Snapshot trees are `0700` directories holding `0600` files (spec 3.6).

    A snapshot is a second plaintext copy of the user's own data. Copying
    inherits nothing from the source's mode, and source stores are often group
    or world readable, so each adapter has to tighten what it writes.

    Every branch below is an assertion rather than a skip: all nine adapters
    ship a success fixture that discovers, and a skip here would silently stop
    checking the adapter whose snapshot handling changed.
    """
    adapter = create_adapter(adapter_id)
    outcome = adapter.discover(_context(_prepared_home(adapter_id, tmp_path)))
    found = [record for record in outcome.records if record.accessibility is Accessibility.FOUND]
    assert found, f"{adapter_id} found nothing in its own success fixture"
    record = found[0]
    source = outcome.instance_paths[record.instance_key]
    target = tmp_path / "snap"

    adapter.snapshot(record, Path(source), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    files = [path for path in target.rglob("*") if path.is_file()]
    assert files, f"{adapter_id} snapshotted no file, so the modes below prove nothing"
    wrong = {
        path.relative_to(target).as_posix(): oct(stat.S_IMODE(path.stat().st_mode))
        for path in sorted(target.rglob("*"))
        if stat.S_IMODE(path.stat().st_mode) != (0o700 if path.is_dir() else 0o600)
    }
    assert not wrong, f"{adapter_id} left readable snapshot entries: {wrong}"


def _utterance(
    utterance_id: str, *, adapter: str, stamp: datetime | None, text: str
) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=utterance_id,
        source_adapter=adapter,
        adapter_version="1.0.0",
        session_hash="a" * 64,
        timestamp=stamp,
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="test",
        source_path_hash=f"{adapter[0]}" * 64,
    )


def test_period_bounds_accept_a_timezone_unknown_timestamp() -> None:
    """Aider records naive local time; the period bounds are always aware.

    Without a shared reading of a naive timestamp this raises inside
    ``collect``, which catches every exception per source, so the whole Aider
    corpus disappears from the run with only a class name recorded.
    """
    naive = _utterance("u-naive", adapter="aider", stamp=datetime(2026, 5, 1, 10, 0), text="hello")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)

    assert _within_bounds(naive, start=start, end=end, cutoff=end) is True
    outside = _utterance("u-old", adapter="aider", stamp=datetime(2020, 1, 1, 10, 0), text="hello")
    assert _within_bounds(outside, start=start, end=end, cutoff=end) is False


def test_dedup_compares_naive_and_aware_timestamps_on_one_scale() -> None:
    """A mixed corpus is the normal case: Aider plus any other adapter."""
    moment = datetime(2026, 5, 1, 10, 0)
    dictated = _utterance(
        "u-wispr", adapter="wispr_flow", stamp=moment.replace(tzinfo=UTC), text="Please explain me"
    )
    typed = _utterance("u-aider", adapter="aider", stamp=moment, text="Please explain me")

    outcome = dedupe([dictated, typed])

    assert len(outcome.canonical) == 1
    assert [exclusion.kind for exclusion in outcome.excluded] == ["exact"]
