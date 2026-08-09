"""Bounded parallel execution for local discovery.

Discovery time is dominated by JSON parsing, which holds the GIL. Measured on
the reference machine (14 cores) over the real Codex corpus of 6,436 rollout
files: 21.5 s with one thread, 22.0 s with fourteen threads, 3.8 s with
fourteen worker processes. Parsing therefore fans out over processes, and
threads are used only where the work is a filesystem walk or a wait on the
process pool.

Two rules keep the fan-out safe:

- A worker returns aggregate values only. Source text never crosses a process
  boundary, so it cannot reach a parent log, an exception message, or stdout.
- Results come back in input order, never completion order. Opaque labels are
  numbered from a sorted list, so a race must never renumber an instance.

Worker functions and their arguments must be picklable module-level objects:
the pool uses the ``spawn`` start method on every platform, because forking a
process that already has threads is unsafe and discovery runs adapters on
threads.
"""

import multiprocessing
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager

# Overridable for tests and for users on unusual machines. A value of 1 keeps
# every stage inline, which is also what fixtures-sized inputs get by default.
WORKER_COUNT_ENV = "GLITE_DISCOVERY_WORKERS"

# Never unbounded: a 128-core machine gains nothing from 128 readers competing
# for one disk, and each worker costs a spawned interpreter.
WORKER_CAP = 16

# Below this many items the pool costs more than it saves (~0.2 s of spawn),
# so small corpora and fixtures stay inline unless a caller asks otherwise.
PARALLEL_THRESHOLD = 24

# Small chunks beat large ones because rollout files differ in size by orders
# of magnitude: measured 3.8 s at 4, 4.4 s at 16, 6.9 s at 32.
_CHUNK_SIZE = 4

_START_METHOD = "spawn"

_pool_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_leases = 0


def _requested_workers(environ: Mapping[str, str] | None) -> int | None:
    """The explicit worker count from the environment, if it names a valid one."""
    raw = (environ or {}).get(WORKER_COUNT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _machine_workers() -> int:
    return os.cpu_count() or 1


def worker_count(*, item_count: int, environ: Mapping[str, str] | None = None) -> int:
    """How many processes to use for ``item_count`` parsing jobs.

    Returns 1 for small inputs so that fixtures and tiny corpora never pay for
    process startup. An explicit :data:`WORKER_COUNT_ENV` value overrides the
    threshold, which is how tests exercise the parallel path on small inputs.
    """
    requested = _requested_workers(environ)
    if requested is None:
        if item_count < PARALLEL_THRESHOLD:
            return 1
        requested = _machine_workers()
    return max(1, min(requested, item_count, WORKER_CAP))


def thread_count(*, item_count: int, environ: Mapping[str, str] | None = None) -> int:
    """How many threads to use for ``item_count`` I/O-bound jobs.

    Threads cost almost nothing to start, so there is no size threshold.
    """
    requested = _requested_workers(environ)
    if requested is None:
        requested = _machine_workers()
    return max(1, min(requested, item_count, WORKER_CAP))


@contextmanager
def _leased_pool(workers: int) -> Iterator[ProcessPoolExecutor]:
    """Lend the one shared process pool, creating it on first use.

    Adapters run concurrently and each fans out over its own work, so without
    sharing they would multiply the machine's worker count by the adapter
    count. The first lease fixes the pool size; later leases join it. The pool
    is shut down as soon as the last caller leaves, so no worker outlives the
    discovery pass that created it.
    """
    global _pool, _pool_leases
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context(_START_METHOD),
            )
        pool = _pool
        _pool_leases += 1
    try:
        yield pool
    finally:
        closing: ProcessPoolExecutor | None = None
        with _pool_lock:
            _pool_leases -= 1
            if _pool_leases == 0:
                _pool = None
                closing = pool
        if closing is not None:
            # cancel_futures drops work that never started; wait reaps the
            # workers, so an interrupt cannot leave orphans behind.
            closing.shutdown(wait=True, cancel_futures=True)


def map_in_processes[T, R](
    function: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int,
) -> list[R]:
    """Apply ``function`` to every item in a bounded pool, in input order.

    Falls back to an inline loop for one worker or fewer than two items. An
    exception raised in a worker propagates to the caller after the remaining
    work is cancelled.
    """
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]
    with _leased_pool(workers) as pool:
        return list(pool.map(function, items, chunksize=_CHUNK_SIZE))


def map_in_threads[T, R](
    function: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int,
) -> list[R]:
    """Apply ``function`` to every item on a thread pool, in input order."""
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, items))


def pool_is_active() -> bool:
    """True while the shared pool exists; tests assert it never outlives a call."""
    with _pool_lock:
        return _pool is not None
