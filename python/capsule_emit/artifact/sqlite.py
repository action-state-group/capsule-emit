# SPDX-License-Identifier: Apache-2.0
"""SQLite artifact store on the standard library's ``sqlite3``.

Same tables and rows as the Go and TypeScript SQLite stores. The caller owns
the connection: open it with ``PRAGMA foreign_keys=ON`` and a busy timeout, and
leave ``autocommit`` at its legacy default (or ``isolation_level=None``): this
store issues its own ``BEGIN``/``COMMIT`` for :meth:`put`, :meth:`get` and
:meth:`purge`. The file may also hold a CLL SQLite log in its own tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, TypeVar

from ._sql import SqlArtifactStore
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
"""

T = TypeVar("T")


class SqliteArtifactStore(SqlArtifactStore):
    """Implements the store contract and its transactional extension
    (:meth:`put_tx`, :meth:`get_tx`, where ``tx`` is a ``sqlite3.Connection``
    with an open transaction). ``lock`` changes nothing here: SQLite already
    serializes writers."""

    _now = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

    def __init__(self, connection: sqlite3.Connection, namespace: str, trusted: Any) -> None:
        super().__init__(connection, namespace, trusted)

    def _is_duplicate(self, error: BaseException) -> bool:
        # The only constraint the capsule insert can violate is its primary key.
        return isinstance(error, sqlite3.IntegrityError)

    def init(self) -> None:
        """Creates the v1 tables if absent; existing tables are untouched."""
        self._conn.executescript(SCHEMA)

    def _transaction(self, run: Callable[[], T]) -> T:
        self._conn.execute("BEGIN")
        try:
            result = run()
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()
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
