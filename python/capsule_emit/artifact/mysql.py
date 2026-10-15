# SPDX-License-Identifier: Apache-2.0
"""MySQL 8.4/InnoDB artifact store on PyMySQL (``pip install capsule-emit[mysql]``).

Same tables and rows as the Go and TypeScript MySQL stores. The caller owns the
connection, its credentials and TLS; a store uses one connection, so give each
thread its own store. Configure ``max_allowed_packet`` above 8 MiB.

Importing this module does not import PyMySQL; the store only needs a DB-API
connection with the ``format`` paramstyle whose errors carry the MySQL error
number as ``args[0]`` (PyMySQL, mysqlclient).
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from ._sql import SqlArtifactStore
from .record import InvalidError, Record, valid_id

__all__ = ["SCHEMA", "MysqlArtifactStore"]

# Identical to go/artifact/mysql/schema.sql and ts/src/artifact/mysql.ts.
SCHEMA = """CREATE TABLE IF NOT EXISTS capsule_store_capsules (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_bytes LONGBLOB NOT NULL,
 producer_envelope BLOB NOT NULL,
 record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY (namespace, capsule_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS capsule_store_artifacts (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 digest_field VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_bytes LONGBLOB NULL,
 content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 retention_state ENUM('present', 'purged', 'never_retained') NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 purged_at DATETIME(6) NULL,
 PRIMARY KEY (namespace, capsule_id, name),
 CONSTRAINT capsule_store_artifacts_parent FOREIGN KEY (namespace, capsule_id)
   REFERENCES capsule_store_capsules (namespace, capsule_id),
 CONSTRAINT capsule_store_artifacts_content CHECK (
   (retention_state = 'present' AND content_bytes IS NOT NULL AND CHAR_LENGTH(content_sha256) = 64 AND purged_at IS NULL)
   OR (retention_state = 'purged' AND content_bytes IS NULL AND CHAR_LENGTH(content_sha256) = 64 AND purged_at IS NOT NULL)
   OR (retention_state = 'never_retained' AND content_bytes IS NULL AND content_sha256 = '' AND purged_at IS NULL)
 )
) ENGINE=InnoDB;
"""

_DUPLICATE_KEY = 1062
_DEADLOCK = 1213
_MAX_DEADLOCK_RETRIES = 4

T = TypeVar("T")


def _errno(error: BaseException) -> Any:
    args = getattr(error, "args", ())
    return args[0] if args else None


class MysqlArtifactStore(SqlArtifactStore):
    """Implements the store contract and its transactional extension
    (:meth:`put_tx`, :meth:`get_tx`, where ``tx`` is this store's connection
    inside a transaction the caller began). ``get_tx(..., lock=True)`` reads
    with ``FOR UPDATE``."""

    _created_at = "UTC_TIMESTAMP(6)"
    _now = "UTC_TIMESTAMP(6)"

    def _sql(self, statement: str) -> str:
        return statement.replace("%", "%%").replace("?", "%s")

    def _is_duplicate(self, error: BaseException) -> bool:
        return _errno(error) == _DUPLICATE_KEY

    def _lock_clause(self, lock: bool) -> str:
        return " FOR UPDATE" if lock else ""

    def init(self) -> None:
        """Creates the v1 tables if absent. Run during provisioning, never inside
        an application transaction: MySQL DDL commits implicitly."""
        with self._conn.cursor() as cursor:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    cursor.execute(statement)

    def _transaction(self, run: Callable[[], T], read_only: bool = False) -> T:
        with self._conn.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY" if read_only else "START TRANSACTION")
        try:
            result = run()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return result

    def put(self, record: Record) -> None:
        """Admits a record in its own transaction, retrying an InnoDB deadlock at
        most four times with bounded backoff. :meth:`put_tx` never retries."""
        for attempt in range(_MAX_DEADLOCK_RETRIES + 1):
            try:
                self._transaction(lambda: self.put_tx(self._conn, record))
                return
            except Exception as exc:
                if _errno(exc) != _DEADLOCK or attempt == _MAX_DEADLOCK_RETRIES:
                    raise
            time.sleep((1 << attempt) * 0.01)

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        return self._transaction(lambda: self._read(self._conn, capsule_id, False), read_only=True)

    def purge(self, capsule_id: str) -> None:
        """Erases present originals. Requires only the record's existence in the
        namespace, not its integrity or signer."""
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        self._transaction(lambda: self._purge_tx(self._conn, capsule_id))
