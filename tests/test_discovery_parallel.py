"""Discovery parallelism: bounds, determinism, isolation, and text safety.

Every corpus here is synthetic. The sentences are written to look like a
learner's English so the adapters keep them, but no real user data appears.
"""

import json
import multiprocessing
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit.adapters.claude_code.adapter import ClaudeCodeAdapter
from glite_english_audit.adapters.codex.adapter import CodexAdapter
from glite_english_audit.artifacts.enums import OsEnvironment, Stability
from glite_english_audit.artifacts.models import NormalizedUtterance, SourceInstanceRecord
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery import inventory, parallel, registry
from glite_english_audit.discovery.base import (
    DiscoveryContext,
    DiscoveryOutcome,
    SnapshotCapture,
    SourceAdapter,
)

# A string that exists nowhere else in the tree, so finding it anywhere proves
# a leak rather than a coincidence.
SENTINEL = "zqx-leak-canary-7412"

_SENTENCES = (
    "Please explain me how this deploy script is working.",
    "I very like this approach, let us continue with it.",
    "How I can make this test more stable than now?",
    "I did not received the error message, what we should check first?",
)


# -- synthetic corpora -------------------------------------------------------


def _codex_home(root: Path, *, sessions: int, sentinel_in: int | None = None) -> Path:
    """A Codex home with ``sessions`` rollout files on distinct days."""
    home = root / "codex-home"
    base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for index in range(sessions):
        stamp = base + timedelta(days=index, minutes=index)
        day_dir = home / ".codex" / "sessions" / f"{stamp:%Y}" / f"{stamp:%m}" / f"{stamp:%d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"0193a1b2-0000-7000-8000-{index:012d}"
        name = f"rollout-{stamp:%Y-%m-%dT%H-%M-%S}-{session_id}.jsonl"
        lines = [
            {
                "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                    "cli_version": "0.150.0",
                    "cwd": "/home/fake-user/projects/site",
                    "originator": "codex_cli_rs",
                    "history_mode": "paginated",
                },
            }
        ]
        for position, sentence in enumerate(_SENTENCES):
            text = sentence
            if sentinel_in is not None and index == sentinel_in and position == 0:
                text = f"{sentence} {SENTINEL}"
            lines.append(
                {
                    "timestamp": (stamp + timedelta(minutes=position + 1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "type": "event_msg",
                    "payload": {"type": "user_message", "kind": "plain", "message": text},
                }
            )
        (day_dir / name).write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )
    return home


def _claude_home(root: Path, *, projects: int, sentinel_in: int | None = None) -> Path:
    """A Claude Code home with ``projects`` project directories."""
    home = root / "claude-home"
    base = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
    for index in range(projects):
        # Distinct earliest timestamps make the label order strict, so a race
        # in the scan would have to show up as a renumbering.
        start = base + timedelta(days=index)
        project = home / ".claude" / "projects" / f"-home-tester-project-{index:03d}"
        project.mkdir(parents=True, exist_ok=True)
        session_id = f"{index:08d}-1111-4111-8111-111111111111"
        records = []
        for position, sentence in enumerate(_SENTENCES):
            text = sentence
            if sentinel_in is not None and index == sentinel_in and position == 0:
                text = f"{sentence} {SENTINEL}"
            records.append(
                {
                    "uuid": f"u{index}-{position}",
                    "parentUuid": None,
                    "timestamp": (start + timedelta(minutes=position))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "sessionId": session_id,
                    "cwd": f"/home/tester/project-{index:03d}",
                    "version": "2.1.210",
                    "gitBranch": "main",
                    "userType": "external",
                    "entrypoint": "cli",
                    "isSidechain": False,
                    "type": "user",
                    "message": {"role": "user", "content": text},
                }
            )
        (project / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
    return home


def _context(home: Path, workers: int | None = None) -> DiscoveryContext:
    environ = {} if workers is None else {parallel.WORKER_COUNT_ENV: str(workers)}
    return DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=home,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        environ=environ,
    )


def _dump(records: list[SourceInstanceRecord]) -> list[dict[str, object]]:
    return [record.model_dump(mode="json") for record in records]


# -- pool workers (module level so the spawn start method can import them) ---


def _square_after_reverse_delay(value: int) -> int:
    """Finish in reverse order: later inputs return first."""
    time.sleep(0.01 * (8 - (value % 8)))
    return value * value


def _raise_with_sentinel(value: int) -> int:
    if value == 3:
        msg = f"worker refused the text {SENTINEL}"
        raise ValueError(msg)
    return value


# -- worker-count bounds -----------------------------------------------------


def test_worker_count_never_exceeds_the_cap_or_the_machine() -> None:
    resolved = parallel.worker_count(item_count=1_000_000, environ={})
    assert 1 <= resolved <= parallel.WORKER_CAP
    assert resolved <= (os.cpu_count() or 1)


def test_worker_count_stays_inline_for_small_inputs() -> None:
    assert parallel.worker_count(item_count=parallel.PARALLEL_THRESHOLD - 1, environ={}) == 1
    assert parallel.worker_count(item_count=0, environ={}) == 1


def test_worker_count_is_overridable_and_bounded_by_the_work() -> None:
    environ = {parallel.WORKER_COUNT_ENV: "3"}
    assert parallel.worker_count(item_count=1000, environ=environ) == 3
    # Never more workers than items, whatever the override asks for.
    assert parallel.worker_count(item_count=2, environ={parallel.WORKER_COUNT_ENV: "9"}) == 2


@pytest.mark.parametrize("raw", ["", "0", "-4", "many", "3.5"])
def test_worker_count_ignores_unusable_overrides(raw: str) -> None:
    environ = {parallel.WORKER_COUNT_ENV: raw}
    assert parallel.worker_count(item_count=2, environ=environ) == 1
    assert parallel.thread_count(item_count=9, environ=environ) >= 1


def test_thread_count_has_no_size_threshold_but_is_still_bounded() -> None:
    resolved = parallel.thread_count(item_count=9, environ={})
    assert 1 <= resolved <= 9
    assert parallel.thread_count(item_count=9, environ={parallel.WORKER_COUNT_ENV: "1"}) == 1


# -- ordering, errors, and orphans -------------------------------------------


def test_process_results_follow_input_order_not_completion_order() -> None:
    items = list(range(24))
    results = parallel.map_in_processes(_square_after_reverse_delay, items, workers=6)
    assert results == [value * value for value in items]


def test_one_worker_runs_inline_and_agrees_with_the_pool() -> None:
    items = list(range(12))
    assert parallel.map_in_processes(_square_after_reverse_delay, items, workers=1) == [
        value * value for value in items
    ]


def test_worker_failure_propagates_without_leaking_text_or_workers() -> None:
    before = len(multiprocessing.active_children())
    with pytest.raises(ValueError) as caught:
        parallel.map_in_processes(_raise_with_sentinel, list(range(24)), workers=4)
    # The caller sees the failure; the pool does not outlive the call.
    assert not parallel.pool_is_active()
    assert str(caught.value)
    deadline = time.monotonic() + 5.0
    while len(multiprocessing.active_children()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(multiprocessing.active_children()) <= before


def test_the_shared_pool_is_gone_after_a_successful_call() -> None:
    parallel.map_in_processes(_square_after_reverse_delay, list(range(8)), workers=4)
    assert not parallel.pool_is_active()


# -- adapter determinism -----------------------------------------------------


def test_codex_inventory_is_identical_at_every_worker_count(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, sessions=40)
    sequential = CodexAdapter().discover(_context(home, workers=1))
    for workers in (2, 4, 7):
        parallel_outcome = CodexAdapter().discover(_context(home, workers=workers))
        assert _dump(parallel_outcome.records) == _dump(sequential.records)


def test_claude_code_inventory_is_identical_at_every_worker_count(tmp_path: Path) -> None:
    home = _claude_home(tmp_path, projects=30)
    sequential = ClaudeCodeAdapter().discover(_context(home, workers=1))
    for workers in (2, 5):
        parallel_outcome = ClaudeCodeAdapter().discover(_context(home, workers=workers))
        assert _dump(parallel_outcome.records) == _dump(sequential.records)


def test_opaque_labels_never_renumber_under_a_race(tmp_path: Path) -> None:
    """Labels come from a documented sort, so worker order must not move them."""
    home = _claude_home(tmp_path, projects=30)
    expected = {
        record.opaque_label: record.instance_key
        for record in ClaudeCodeAdapter().discover(_context(home, workers=1)).records
    }
    assert "Claude Code 4" in expected
    for _ in range(3):
        records = ClaudeCodeAdapter().discover(_context(home, workers=6)).records
        assert {record.opaque_label: record.instance_key for record in records} == expected


def test_parallel_scanning_keeps_the_opened_path_audit(tmp_path: Path) -> None:
    """Paths opened inside a worker still reach the adapter's audit log."""
    home = _claude_home(tmp_path, projects=30)
    adapter = ClaudeCodeAdapter()
    adapter.discover(_context(home, workers=4))
    opened = adapter._opened_paths
    assert len(opened) == 30
    assert all(path.suffix == ".jsonl" for path in opened)


# -- adapter isolation inside run_discovery ----------------------------------


class _StubAdapter:
    """A minimal adapter that yields one record and no paths.

    Implements the whole :class:`SourceAdapter` protocol, not just the part
    these tests call: a partial stub would type-check as a different thing
    from what ``run_discovery`` is handed at runtime.
    """

    def __init__(self, adapter_id: str, *, delay: float = 0.0) -> None:
        self._adapter_id = adapter_id
        self._delay = delay

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def adapter_version(self) -> str:
        return "1.0.0"

    @property
    def stability(self) -> Stability:
        return Stability.STABLE

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        time.sleep(self._delay)
        return DiscoveryOutcome(records=[], instance_paths={f"{self._adapter_id}-key": Path(".")})

    def snapshot(
        self, instance: SourceInstanceRecord, source_path: Path, target_dir: Path
    ) -> SnapshotCapture:
        return SnapshotCapture(snapshot_relative_dir=self._adapter_id, files=[])

    def extract(
        self, instance: SourceInstanceRecord, snapshot_dir: Path
    ) -> Iterator[NormalizedUtterance]:
        return iter(())

    def verify(
        self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
    ) -> list[Diagnostic]:
        return []


class _ExplodingAdapter(_StubAdapter):
    """An adapter whose failure message quotes the source text that broke it."""

    def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
        msg = f"cannot parse record containing {SENTINEL}"
        raise RuntimeError(msg)


@pytest.fixture
def clean_registry() -> Iterator[None]:
    saved = dict(registry._FACTORIES)
    registry._FACTORIES.clear()
    try:
        yield
    finally:
        registry._FACTORIES.clear()
        registry._FACTORIES.update(saved)


def _register(adapter_id: str, factory: Callable[[], SourceAdapter]) -> None:
    registry.register_adapter(adapter_id, factory)


@pytest.mark.usefixtures("clean_registry")
def test_one_failing_adapter_never_stops_the_others(tmp_path: Path) -> None:
    _register("aaa_good", lambda: _StubAdapter("aaa_good"))
    _register("bbb_broken", lambda: _ExplodingAdapter("bbb_broken"))
    _register("ccc_good", lambda: _StubAdapter("ccc_good"))

    report = inventory.run_discovery(_context(tmp_path))

    surviving = [key for outcome in report.outcomes for key in outcome.instance_paths]
    assert surviving == ["aaa_good-key", "ccc_good-key"]
    assert [diagnostic.item_ref for diagnostic in report.failures] == ["bbb_broken"]
    assert report.failures[0].code == "SOURCE_DISCOVERY_FAILED"


@pytest.mark.usefixtures("clean_registry")
def test_a_failing_factory_is_recorded_like_a_failing_scan(tmp_path: Path) -> None:
    def _broken_factory() -> SourceAdapter:
        msg = "factory refused to build"
        raise ImportError(msg)

    _register("aaa_good", lambda: _StubAdapter("aaa_good"))
    _register("bbb_broken", _broken_factory)

    report = inventory.run_discovery(_context(tmp_path))

    assert len(report.outcomes) == 1
    assert [diagnostic.item_ref for diagnostic in report.failures] == ["bbb_broken"]


@pytest.mark.usefixtures("clean_registry")
def test_adapter_failures_never_carry_the_text_that_caused_them(tmp_path: Path) -> None:
    _register("bbb_broken", lambda: _ExplodingAdapter("bbb_broken"))

    report = inventory.run_discovery(_context(tmp_path))

    rendered = json.dumps([diagnostic.model_dump(mode="json") for diagnostic in report.failures])
    assert SENTINEL not in rendered
    assert "RuntimeError" in rendered


@pytest.mark.usefixtures("clean_registry")
def test_outcomes_follow_registry_order_not_completion_order(tmp_path: Path) -> None:
    # The slowest adapter sorts first, so completion order is the reverse of
    # the order the report must present.
    _register("aaa_slow", lambda: _StubAdapter("aaa_slow", delay=0.20))
    _register("bbb_medium", lambda: _StubAdapter("bbb_medium", delay=0.10))
    _register("ccc_fast", lambda: _StubAdapter("ccc_fast"))

    report = inventory.run_discovery(_context(tmp_path))

    keys = [key for outcome in report.outcomes for key in outcome.instance_paths]
    assert keys == ["aaa_slow-key", "bbb_medium-key", "ccc_fast-key"]


# -- text never reaches stdout ----------------------------------------------


@pytest.mark.usefixtures("clean_registry")
def test_the_inventory_command_prints_no_source_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole CLI, over a synthetic home, must print aggregates only."""
    home = _codex_home(tmp_path, sessions=30, sentinel_in=7)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv(parallel.WORKER_COUNT_ENV, "4")
    run_dir = tmp_path / "private"

    assert inventory.main(["--run-dir", str(run_dir)]) == 0

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err
    payload = json.loads(captured.out)
    assert set(payload) == {"inventory"}
    codex_rows = [row for row in payload["inventory"] if row["adapter_id"] == "codex"]
    assert codex_rows and codex_rows[0]["candidate_messages"] == 30 * len(_SENTENCES)
    assert SENTINEL not in (run_dir / "source-inventory.json").read_text(encoding="utf-8")
