# SPDX-License-Identifier: Apache-2.0
"""Per-scope single-writer locking for evaluate-and-reserve.

Appending a line to the ledger file is not, by itself, business atomicity:
two concurrent evaluate-and-reserve calls for the same scope can each read
the current aggregate, each see room under the cap, and each append a
reservation — over-reserving the scope even though every individual append
was itself a well-formed write. This is the serialization boundary that
closes that race: the read (current aggregate), the decide (cap check), and
the append (reservation) run as one atomic critical section per scope, so N
concurrent calls for the same scope can never jointly over-reserve it.

A scope is (cap identity, subject identity) — narrower than the whole
ledger: two different subjects, or two different caps, don't contend on
each other's lock. This is a local, single-process primitive; a distributed
sequencer across processes/nodes is a deployment-specific concern, not this
module's.
"""
from __future__ import annotations

import threading

__all__ = ["ScopeKey", "ScopeLocks"]

# (cap identity e.g. action_class, subject identity e.g. developer)
ScopeKey = tuple[str, str]


class ScopeLocks:
    """A registry of per-scope locks, created lazily on first use.

    Locks are never removed — bounded by the number of distinct scope pairs
    ever seen by this process, which is acceptable for a v0 (an eviction
    policy is a later concern).
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[ScopeKey, threading.Lock] = {}

    def get(self, scope: ScopeKey) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(scope)
            if lock is None:
                lock = threading.Lock()
                self._locks[scope] = lock
            return lock
