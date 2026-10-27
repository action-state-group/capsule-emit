# SPDX-License-Identifier: Apache-2.0
"""In-memory artifact store, for tests and short-lived tools.

Follows the contract in ``docs/artifact-store.md`` but keeps nothing across
process restarts and has no transactional extension. It implements the index
extension (:class:`capsule_emit.artifact.index.IndexStore`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping

from .index import DEFAULT_FIND_LIMIT, MemoryIndex, Page, normalize_paths
from .record import (
    ConflictError,
    InvalidError,
    NotFoundError,
    PurgedError,
    Record,
    check_store_config,
    copy_record,
    has_purged,
    prepare,
    purged_copy,
    storage_checksum,
    valid_id,
    verify,
)


class MemoryArtifactStore:
    """Holds prepared records by Capsule ID. Safe for concurrent use."""

    def __init__(self, namespace: str, trusted: Iterable[bytes]) -> None:
        self._trusted = check_store_config(namespace, trusted)
        self._namespace = namespace
        self._records: dict = {}
        self._order: list = []  # Capsule IDs in admission order
        self._lookup = MemoryIndex()
        self._lock = threading.Lock()

    @property
    def namespace(self) -> str:
        return self._namespace

    def init(self) -> None:
        """Has nothing to provision."""

    def put(self, record: Record) -> None:
        prepared = prepare(record)
        verify(prepared, self._trusted)
        checksum = storage_checksum(prepared)
        with self._lock:
            existing = self._records.get(prepared.capsule_id)
            if existing is None:
                self._records[prepared.capsule_id] = prepared
                self._order.append(prepared.capsule_id)
                self._lookup.add(prepared.capsule_id, prepared.capsule)
                return
        # existing was verified with these same keys when it was admitted, and
        # memory cannot be altered behind the store, so it needs no re-verification.
        if storage_checksum(existing) != checksum:
            raise ConflictError("immutable record conflict")
        if has_purged(existing):
            raise PurgedError("originals were purged")

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock:
            stored = self._records.get(capsule_id)
        if stored is None:
            raise NotFoundError("capsule not found")
        record = copy_record(stored)
        verify(record, self._trusted)
        return record

    def purge(self, capsule_id: str) -> None:
        """Replaces each present original with a tombstone that keeps its SHA-256.
        Requires only the record's existence, not its verification."""
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock:
            stored = self._records.get(capsule_id)
            if stored is None:
                raise NotFoundError("capsule not found")
            self._records[capsule_id] = purged_copy(stored)

    def init_index(self, paths: Iterable[str]) -> None:
        """Declares paths and indexes the records already stored."""
        with self._lock:
            if self._lookup.declare(paths):
                self._index_all()

    def reindex(self, paths: Iterable[str]) -> None:
        """Replaces the declared paths and rebuilds the index."""
        normalized = normalize_paths(paths)
        with self._lock:
            self._lookup.reset(normalized)
            self._index_all()

    def _index_all(self) -> None:
        for capsule_id in self._order:
            self._lookup.add(capsule_id, self._records[capsule_id].capsule)

    def find(
        self,
        where: Mapping[str, str] | None = None,
        since: str | None = None,
        after: str | None = None,
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> Page:
        """Returns one page of IDs matching the query in admission order."""
        with self._lock:
            return self._lookup.find(where, since, after, limit)
