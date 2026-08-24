# SPDX-License-Identifier: Apache-2.0
"""One log, one writer (O16 audit item 12, frozen surface §7d).

``append_to_ledger`` takes an OS-level flock on a sidecar ``<ledger>.lock``
file for the duration of one append. A second writer that finds the lock
held fails immediately with :class:`LedgerLockedError` naming the holder
(never a silent interleave); waiting is opt-in via ``wait=True``.

The holder in these tests is always ``ledger._writer_lock`` called directly,
never ``append_to_ledger`` -- routing the holder through ``append_to_ledger``
would let the *thread*-level ``_append_lock`` (a single lock shared by every
ledger, unrelated to flock) serialize the two sides for free and the test
would pass without ever exercising the flock path.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time

import pytest

from capsule_emit.ledger import LedgerLockedError, _writer_lock, append_to_ledger, read_ledger


@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# Baseline: no contention
# ---------------------------------------------------------------------------

def test_single_writer_appends_and_cleans_up_lock_file(tmp_ledger):
    append_to_ledger({"capsule_id": "c1"}, tmp_ledger)
    assert read_ledger(tmp_ledger) == [{"capsule_id": "c1"}]
    lock_file = tmp_ledger.with_name(tmp_ledger.name + ".lock")
    assert lock_file.exists()  # sidecar persists; only the flock itself is released


# ---------------------------------------------------------------------------
# Same-process, independent-fd contention (flock is per open-file-description,
# not per-process -- this exercises the real kernel-level exclusion fast,
# without paying for a subprocess).
# ---------------------------------------------------------------------------

def test_second_writer_fails_immediately_while_lock_held(tmp_ledger):
    with _writer_lock(tmp_ledger):
        start = time.monotonic()
        with pytest.raises(LedgerLockedError):
            append_to_ledger({"capsule_id": "c2"}, tmp_ledger)
        elapsed = time.monotonic() - start

    assert elapsed < 1.0, "default (wait=False) must fail immediately, not block"
    assert read_ledger(tmp_ledger) == []  # the blocked write never landed


def test_locked_error_names_holder_and_points_at_doc(tmp_ledger):
    with _writer_lock(tmp_ledger):
        with pytest.raises(LedgerLockedError) as exc_info:
            append_to_ledger({"capsule_id": "c2"}, tmp_ledger)

    message = str(exc_info.value)
    assert f"pid {__import__('os').getpid()}" in message
    assert "docs/concurrency.md" in message


def test_lock_released_after_append_lets_next_writer_through(tmp_ledger):
    with _writer_lock(tmp_ledger):
        pass  # acquired and released
    append_to_ledger({"capsule_id": "c3"}, tmp_ledger)
    assert read_ledger(tmp_ledger) == [{"capsule_id": "c3"}]


# ---------------------------------------------------------------------------
# wait=True: opt-in blocking, bounded by an optional timeout
# ---------------------------------------------------------------------------

def test_wait_true_blocks_until_holder_releases(tmp_ledger):
    release = threading.Event()
    entered = threading.Event()

    def hold():
        with _writer_lock(tmp_ledger):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(timeout=5), "holder thread never acquired the lock"

    def release_soon():
        time.sleep(0.2)
        release.set()

    threading.Thread(target=release_soon).start()

    start = time.monotonic()
    append_to_ledger({"capsule_id": "c4"}, tmp_ledger, wait=True)
    elapsed = time.monotonic() - start

    holder.join(timeout=5)
    assert elapsed >= 0.15, "wait=True must actually wait for the holder, not race it"
    assert read_ledger(tmp_ledger) == [{"capsule_id": "c4"}]


def test_wait_with_timeout_raises_after_deadline_names_wait(tmp_ledger):
    release = threading.Event()
    entered = threading.Event()

    def hold():
        with _writer_lock(tmp_ledger):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(timeout=5)

    try:
        with pytest.raises(LedgerLockedError) as exc_info:
            append_to_ledger({"capsule_id": "c5"}, tmp_ledger, wait=True, timeout=0.2)
        assert "after waiting 0.2s" in str(exc_info.value)
    finally:
        release.set()
        holder.join(timeout=5)

    assert read_ledger(tmp_ledger) == []


# ---------------------------------------------------------------------------
# The real two-process test the audit calls for -- prior concurrency tests
# were thread-level only. flock exclusion is per open-file-description, so a
# fork()-inherited fd would (incorrectly) share the lock; the holder here
# always opens its own fresh fd inside the child, which is what makes this a
# genuine test of cross-process (not just cross-fd) exclusion.
# ---------------------------------------------------------------------------

def _hold_lock_in_subprocess(ledger_path: str, ready_queue, release_event) -> None:
    from capsule_emit.ledger import _writer_lock  # re-imported in the child

    with _writer_lock(__import__("pathlib").Path(ledger_path)):
        ready_queue.put(True)
        release_event.wait(timeout=10)


def test_two_real_processes_second_fails_then_succeeds_after_release(tmp_ledger):
    ctx = mp.get_context("spawn")
    ready_queue: mp.Queue = ctx.Queue()
    release_event = ctx.Event()
    proc = ctx.Process(
        target=_hold_lock_in_subprocess,
        args=(str(tmp_ledger), ready_queue, release_event),
    )
    proc.start()
    try:
        assert ready_queue.get(timeout=10) is True, "subprocess never signaled it held the lock"

        start = time.monotonic()
        with pytest.raises(LedgerLockedError) as exc_info:
            append_to_ledger({"capsule_id": "cross-process"}, tmp_ledger)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, "a second OS process must fail immediately, not block"
        message = str(exc_info.value)
        assert f"pid {proc.pid}" in message
        assert "docs/concurrency.md" in message
        assert read_ledger(tmp_ledger) == []
    finally:
        release_event.set()
        proc.join(timeout=10)

    append_to_ledger({"capsule_id": "after-release"}, tmp_ledger)
    assert read_ledger(tmp_ledger) == [{"capsule_id": "after-release"}]
