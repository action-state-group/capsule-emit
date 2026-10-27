# SPDX-License-Identifier: Apache-2.0
"""SQLite artifact store on the standard library's ``sqlite3``.

Same tables and rows as the Go and TypeScript SQLite stores. The caller owns
the connection: open it with ``isolation_level=None``, ``PRAGMA
foreign_keys=ON`` and a busy timeout. This store issues its own
``BEGIN``/``COMMIT`` for :meth:`put`, :meth:`get` and :meth:`purge`, so the
connection must not be inside a transaction then; with the legacy default
isolation level, sqlite3 opens one implicitly before the caller's own
INSERT/UPDATE, which the caller must commit first. Python 3.12's
``autocommit=False`` mode is not supported. The file may also hold a CLL SQLite
log in its own tables.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any, Callable, TypeVar

from ._sql import SqlArtifactStore
from .index import DEFAULT_FIND_LIMIT, Page, normalize_paths
from .record import InvalidError, Record, valid_id

__all__ = ["SCHEMA", "SqliteArtifactStore"]

# Identical to go/artifact/sqlite/schema.sql and ts/src/artifact/sqlite.ts.
SCHEMA = """CREATE TABLE IF NOT EXISTS capsule_store_capsules (
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 capsule_bytes BLOB NOT NULL,
 producer_envelope BLOB NOT NULL,
 record_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY (namespace, capsule_id)
);

CREATE TABLE IF NOT EXISTS capsule_store_artifacts (
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 name TEXT NOT NULL,
 digest_field TEXT NOT NULL,
 content_bytes BLOB NULL,
 content_sha256 TEXT NOT NULL,
 retention_state TEXT NOT NULL CHECK (retention_state IN ('present','purged','never_retained')),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 purged_at TEXT NULL,
 PRIMARY KEY (namespace, capsule_id, name),
 FOREIGN KEY (namespace, capsule_id) REFERENCES capsule_store_capsules (namespace, capsule_id),
 CHECK (
   (retention_state = 'present' AND content_bytes IS NOT NULL AND length(content_sha256) = 64 AND purged_at IS NULL)
   OR (retention_state = 'purged' AND content_bytes IS NULL AND length(content_sha256) = 64 AND purged_at IS NOT NULL)
   OR (retention_state = 'never_retained' AND content_bytes IS NULL AND content_sha256 = '' AND purged_at IS NULL)
 )
);

CREATE TABLE IF NOT EXISTS capsule_store_index_paths (
 namespace TEXT NOT NULL PRIMARY KEY,
 paths TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capsule_store_index_records (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 ts INTEGER NULL,
 UNIQUE (namespace, capsule_id)
);

CREATE INDEX IF NOT EXISTS capsule_store_index_records_ts ON capsule_store_index_records (namespace, ts);

CREATE TABLE IF NOT EXISTS capsule_store_index (
 namespace TEXT NOT NULL,
 path TEXT NOT NULL,
 value_sha256 TEXT NOT NULL,
 seq INTEGER NOT NULL,
 PRIMARY KEY (namespace, path, value_sha256, seq)
);
"""

T = TypeVar("T")


class SqliteArtifactStore(SqlArtifactStore):
    """Implements the store contract and its transactional extension
    (:meth:`put_tx`, :meth:`get_tx`, where ``tx`` is a ``sqlite3.Connection``
    with an open transaction). ``lock`` changes nothing here: SQLite already
    serializes writers. Also implements the index extension
    (:class:`capsule_emit.artifact.index.IndexStore`)."""

    _now = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    _index_tables_exist = (
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('capsule_store_index_paths','capsule_store_index_records','capsule_store_index')"
    )
    _insert_ignore = "INSERT OR IGNORE INTO"

    def __init__(self, connection: sqlite3.Connection, namespace: str, trusted: Any) -> None:
        super().__init__(connection, namespace, trusted)

    def _is_duplicate(self, error: BaseException) -> bool:
        # The only constraint the capsule insert can violate is its primary key.
        return isinstance(error, sqlite3.IntegrityError)

    def init(self) -> None:
        """Creates the tables if absent; existing tables are untouched. Run
        during provisioning: unlike ``executescript``, it does not commit a
        transaction the caller has open, but the DDL becomes part of it."""
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self._conn.execute(statement)

    def _transaction(self, run: Callable[[], T]) -> T:
        self._conn.execute("BEGIN")
        try:
            result = run()
            self._conn.commit()
        except BaseException:
            # Also after a failed COMMIT (SQLITE_BUSY), which leaves the
            # transaction open with this call's writes in it.
            self._conn.rollback()
            raise
        return result

    def put(self, record: Record) -> None:
        self._transaction(lambda: self.put_tx(self._conn, record))

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        return self._transaction(lambda: self._read(self._conn, capsule_id, False))

    def purge(self, capsule_id: str) -> None:
        """Erases present originals. Requires only the record's existence in the
        namespace, not its integrity or signer."""
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        self._transaction(lambda: self._purge_tx(self._conn, capsule_id))

    def init_index(self, paths: Iterable[str]) -> None:
        """Provisions the tables as :meth:`init` does, then declares paths for
        the namespace and indexes its stored records. The same set again does
        nothing; a different set raises ``ConflictError``."""
        paths = normalize_paths(paths)
        self.init()
        self._transaction(lambda: self._declare_tx(self._conn, paths))

    def reindex(self, paths: Iterable[str]) -> None:
        """Replaces the namespace's declared paths and rebuilds its index."""
        paths = normalize_paths(paths)
        self.init()
        self._transaction(lambda: self._reindex_tx(self._conn, paths))

    def find(
        self,
        where: Mapping[str, str] | None = None,
        since: str | None = None,
        after: str | None = None,
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> Page:
        """Returns one page of IDs matching the query in admission order."""
        return self._transaction(lambda: self._find_tx(self._conn, where, since, after, limit))
