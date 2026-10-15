# SPDX-License-Identifier: Apache-2.0
"""Shared logic of the MySQL and SQLite artifact stores.

Both use the same two tables and the same statements as the Go and TypeScript
SQL backends (``docs/artifact-store.md``), so rows written by any language read
in the others. A dialect supplies the placeholder, timestamp expression,
duplicate-key test and transaction control.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .record import (
    Artifact,
    ConflictError,
    CorruptError,
    InvalidError,
    NotFoundError,
    PurgedError,
    Record,
    check_store_config,
    has_purged,
    prepare,
    storage_checksum,
    valid_id,
    verify,
)


class SqlArtifactStore:
    """Base class; use :class:`MysqlArtifactStore` or :class:`SqliteArtifactStore`."""

    #: SQL that writes ``created_at`` on insert, or "" to use the column default.
    _created_at = ""
    #: SQL expression for ``purged_at``.
    _now = ""

    def __init__(self, connection: Any, namespace: str, trusted: Iterable[bytes]) -> None:
        if connection is None:
            raise InvalidError("store configuration")
        self._trusted = check_store_config(namespace, trusted)
        self._namespace = namespace
        self._conn = connection

    @property
    def namespace(self) -> str:
        return self._namespace

    # Dialect hooks -------------------------------------------------------

    def _sql(self, statement: str) -> str:
        """Rewrites ``?`` placeholders for the driver's paramstyle."""
        return statement

    def _is_duplicate(self, error: BaseException) -> bool:
        raise NotImplementedError

    def _lock_clause(self, lock: bool) -> str:
        return ""

    def _blob(self, value: Any) -> Any:
        """Converts a driver BLOB value to bytes; None stays None."""
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        raise CorruptError("content_bytes is not a BLOB")

    # Contract ------------------------------------------------------------

    def put_tx(self, tx: Any, record: Record) -> None:
        """Admits a record inside the caller's open transaction on ``tx`` (a
        connection to this store's database). Never commits or retries; on any
        error the caller rolls back the whole transaction."""
        if tx is None:
            raise InvalidError("transaction")
        prepared = prepare(record)
        verify(prepared, self._trusted)
        checksum = storage_checksum(prepared)
        cursor = tx.cursor()
        try:
            try:
                columns = "namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256"
                values = "?,?,?,?,?"
                if self._created_at:
                    columns, values = columns + ",created_at", values + "," + self._created_at
                cursor.execute(
                    self._sql(f"INSERT INTO capsule_store_capsules ({columns}) VALUES({values})"),
                    (
                        self._namespace,
                        prepared.capsule_id,
                        prepared.capsule,
                        prepared.producer_envelope,
                        checksum,
                    ),
                )
            except Exception as exc:
                if not self._is_duplicate(exc):
                    raise
                existing = self._read(tx, prepared.capsule_id, True)
                if storage_checksum(existing) != checksum:
                    raise ConflictError("immutable record conflict") from None
                if has_purged(existing):
                    raise PurgedError("originals were purged") from None
                return
            columns = "namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state"
            values = "?,?,?,?,?,?,?"
            if self._created_at:
                columns, values = columns + ",created_at", values + "," + self._created_at
            for a in prepared.artifacts:
                cursor.execute(
                    self._sql(f"INSERT INTO capsule_store_artifacts ({columns}) VALUES({values})"),
                    (
                        self._namespace,
                        prepared.capsule_id,
                        a.name,
                        a.binding,
                        a.content,
                        a.content_sha256,
                        a.state,
                    ),
                )
        finally:
            cursor.close()

    def get_tx(self, tx: Any, capsule_id: str, lock: bool = False) -> Record:
        """Reads inside the caller's open transaction on ``tx``. With ``lock``,
        the read serializes with concurrent writers of the same record."""
        if tx is None:
            raise InvalidError("transaction")
        return self._read(tx, capsule_id, lock)

    def _read(self, tx: Any, capsule_id: str, lock: bool) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        lock_clause = self._lock_clause(lock)
        cursor = tx.cursor()
        try:
            cursor.execute(
                self._sql(
                    "SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules "
                    "WHERE namespace=? AND capsule_id=?" + lock_clause
                ),
                (self._namespace, capsule_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFoundError("capsule not found")
            capsule, envelope, expected = row
            cursor.execute(
                self._sql(
                    "SELECT name,digest_field,content_bytes,content_sha256,retention_state "
                    "FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name" + lock_clause
                ),
                (self._namespace, capsule_id),
            )
            artifacts = [
                Artifact(name=name, binding=binding, content=self._blob(content), content_sha256=sha, state=state)
                for name, binding, content, sha, state in cursor.fetchall()
            ]
        finally:
            cursor.close()
        record = Record(
            capsule_id=capsule_id,
            capsule=self._blob(capsule),
            producer_envelope=self._blob(envelope),
            artifacts=artifacts,
        )
        if storage_checksum(record) != expected:
            raise CorruptError("stored record integrity failure")
        verify(record, self._trusted)
        return record

    def _purge_tx(self, tx: Any, capsule_id: str) -> None:
        cursor = tx.cursor()
        try:
            cursor.execute(
                self._sql(
                    "SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?"
                    + self._lock_clause(True)
                ),
                (self._namespace, capsule_id),
            )
            if cursor.fetchone() is None:
                raise NotFoundError("capsule not found")
            cursor.execute(
                self._sql(
                    "UPDATE capsule_store_artifacts SET content_bytes=NULL,retention_state='purged',"
                    f"purged_at={self._now} WHERE namespace=? AND capsule_id=? AND retention_state='present'"
                ),
                (self._namespace, capsule_id),
            )
        finally:
            cursor.close()
