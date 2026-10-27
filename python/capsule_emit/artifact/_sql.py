# SPDX-License-Identifier: Apache-2.0
"""Shared logic of the MySQL and SQLite artifact stores.

Both use the same tables and the same statements as the Go and TypeScript
SQL backends (``docs/artifact-store.md``), so rows written by any language read
in the others. A dialect supplies the placeholder, timestamp expression,
duplicate-key test, locking clauses and transaction control.

The index extension's SQL lives here too, as in the Go module's
``artifact/internal/index/sql.go``: the declaration read, admission at put in
the same transaction, declaration with backfill, reindex and find.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .index import Page, check_query, decode_paths, encode_paths, extract, normalize_paths, value_key
from .index import cursor as format_cursor
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
    #: Counts the index tables; returns 3 once they exist.
    _index_tables_exist = ""
    #: End a SELECT that must see, and hold, the latest committed rows
    #: (MySQL FOR SHARE / FOR UPDATE; empty for SQLite, which serializes writers).
    _lock_shared = ""
    _lock_update = ""
    #: Starts an insert that skips rows already present.
    _insert_ignore = ""

    def __init__(self, connection: Any, namespace: str, trusted: Iterable[bytes]) -> None:
        if connection is None:
            raise InvalidError("store configuration")
        self._trusted = check_store_config(namespace, trusted)
        self._namespace = namespace
        self._conn = connection
        # Set once the index tables are seen; a database provisioned before
        # they existed has no declaration until init_index runs.
        self._index_tables = False

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
            self._admit(cursor, prepared.capsule_id, prepared.capsule)
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

    # Index extension ---------------------------------------------------------

    def _declared(self, cursor: Any, lock: str) -> list | None:
        """Reads the namespace's declaration, or None when there is none."""
        if not self._index_tables:
            cursor.execute(self._index_tables_exist)
            if cursor.fetchone()[0] < 3:
                return None
            self._index_tables = True
        cursor.execute(
            self._sql("SELECT paths FROM capsule_store_index_paths WHERE namespace=?" + lock), (self._namespace,)
        )
        row = cursor.fetchone()
        return None if row is None else decode_paths(row[0])

    def _admit(self, cursor: Any, capsule_id: str, capsule: bytes) -> None:
        """Indexes a record put_tx has just inserted, when the namespace
        declares paths, in the same transaction."""
        paths = self._declared(cursor, self._lock_shared)
        if paths is not None:
            self._index_add(cursor, paths, capsule_id, capsule)

    def _index_add(self, cursor: Any, paths: list, capsule_id: str, capsule: bytes) -> None:
        extracted = extract(capsule, paths)
        cursor.execute(
            self._sql(f"{self._insert_ignore} capsule_store_index_records (namespace,capsule_id,ts) VALUES(?,?,?)"),
            (self._namespace, capsule_id, extracted.ts),
        )
        if cursor.rowcount == 0:
            return  # already indexed
        seq = cursor.lastrowid
        for path, value in extracted.entries:
            cursor.execute(
                self._sql("INSERT INTO capsule_store_index (namespace,path,value_sha256,seq) VALUES(?,?,?,?)"),
                (self._namespace, path, value_key(value), seq),
            )

    def _declare_tx(self, tx: Any, paths: list) -> None:
        """init_index's declaration, after the caller provisioned the tables."""
        normalized = normalize_paths(paths)
        cursor = tx.cursor()
        try:
            current = self._declared(cursor, self._lock_update)
            if current is not None:
                if current != normalized:
                    raise ConflictError(f"namespace already indexes {encode_paths(current)}")
                return
            cursor.execute(
                self._sql("INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)"),
                (self._namespace, encode_paths(normalized)),
            )
            self._backfill(cursor, normalized)
        finally:
            cursor.close()

    def _reindex_tx(self, tx: Any, paths: list) -> None:
        """Replaces the declaration and rebuilds the index."""
        normalized = normalize_paths(paths)
        cursor = tx.cursor()
        try:
            # Lock the declaration first, so a concurrent put waits for the rebuild.
            try:
                self._declared(cursor, self._lock_update)
            except CorruptError:
                pass
            for table in ("capsule_store_index", "capsule_store_index_records", "capsule_store_index_paths"):
                cursor.execute(self._sql(f"DELETE FROM {table} WHERE namespace=?"), (self._namespace,))
            cursor.execute(
                self._sql("INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)"),
                (self._namespace, encode_paths(normalized)),
            )
            self._backfill(cursor, normalized)
        finally:
            cursor.close()

    def _backfill(self, cursor: Any, paths: list) -> None:
        """Indexes the stored records in creation order. A record whose Capsule
        bytes no longer decode is left out; get reports it as corrupt."""
        cursor.execute(
            self._sql(
                "SELECT capsule_id,capsule_bytes FROM capsule_store_capsules WHERE namespace=? "
                "ORDER BY created_at,capsule_id" + self._lock_shared
            ),
            (self._namespace,),
        )
        rows = cursor.fetchall()
        for capsule_id, capsule in rows:
            try:
                self._index_add(cursor, paths, capsule_id, self._blob(capsule))
            except CorruptError:
                continue

    def _find_tx(self, tx: Any, where: Any, since: Any, after: Any, limit: Any) -> Page:
        cursor = tx.cursor()
        try:
            paths = self._declared(cursor, "")
            if paths is None:
                raise InvalidError("no index paths declared")
            c = check_query(where, since, after, limit, paths)
            if not c.where:
                query = (
                    "SELECT r.seq,r.capsule_id FROM capsule_store_index_records r WHERE r.namespace=? AND r.seq>?"
                )
                args: list = [self._namespace, c.after]
            else:
                first_path, first_value = c.where[0]
                query = (
                    "SELECT r.seq,r.capsule_id FROM capsule_store_index i0 JOIN capsule_store_index_records r "
                    "ON r.seq=i0.seq WHERE i0.namespace=? AND i0.path=? AND i0.value_sha256=? AND i0.seq>?"
                )
                args = [self._namespace, first_path, value_key(first_value), c.after]
                for path, value in c.where[1:]:
                    query += (
                        " AND EXISTS (SELECT 1 FROM capsule_store_index i WHERE i.namespace=r.namespace"
                        " AND i.path=? AND i.value_sha256=? AND i.seq=r.seq)"
                    )
                    args += [path, value_key(value)]
            if c.since is not None:
                query += " AND r.ts>=?"
                args.append(c.since)
            query += " ORDER BY r.seq LIMIT ?"
            args.append(c.limit + 1)
            cursor.execute(self._sql(query), tuple(args))
            rows = cursor.fetchall()
        finally:
            cursor.close()
        page = Page(ids=[capsule_id for _, capsule_id in rows[: c.limit]])
        if len(rows) > c.limit:
            page.next = format_cursor(rows[c.limit - 1][0])
        return page
