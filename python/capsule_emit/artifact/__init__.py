# SPDX-License-Identifier: Apache-2.0
"""Optional artifact store: exact sealed Capsules, Producer Envelopes and
their business originals, with retention and purge.

The contract (``docs/artifact-store.md``) is shared with the Go and TypeScript
stores, and the files and rows each backend writes open in the other
languages. Backends, each in its own module so importing this package loads no
database driver:

- ``capsule_emit.artifact.jsonl.JsonlArtifactStore``: one file per namespace
- ``capsule_emit.artifact.memory.MemoryArtifactStore``: process memory
- ``capsule_emit.artifact.sqlite.SqliteArtifactStore``: the standard ``sqlite3``
- ``capsule_emit.artifact.mysql.MysqlArtifactStore``: MySQL 8.4 via PyMySQL
  (``pip install capsule-emit[mysql]``)

- ``capsule_emit.legacy_ledger.LegacyLedger``: the flat ``ledger.jsonl``
  that ``seal()`` writes, with declared capability limits (no originals, no
  purge); it is also the ledger's CLL entry log

``capsule_emit.artifact.conformance.run`` runs the shared conformance suite
against any :class:`Store`, built in or your own.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .record import (
    AGENT_OUTPUT_DIGEST,
    EFFECT_REQUEST_DIGEST,
    EFFECT_RESPONSE_DIGEST,
    NEVER_RETAINED,
    PAYLOAD_DIGEST,
    PRESENT,
    PURGED,
    Artifact,
    ArtifactError,
    ArtifactVerification,
    ConflictError,
    CorruptError,
    DigestMismatchError,
    InvalidError,
    NotFoundError,
    PurgedError,
    Record,
    UnsupportedError,
    UntrustedSignerError,
    prepare,
    storage_checksum,
    verify,
)


@runtime_checkable
class Store(Protocol):
    """The artifact store contract. Constructors stay per backend."""

    @property
    def namespace(self) -> str:
        """The storage scope the store was constructed with."""

    def init(self) -> None:
        """Provisions storage. Idempotent; never alters existing data."""

    def put(self, record: Record) -> None:
        """Admits an immutable record, accepting byte-identical retries."""

    def get(self, capsule_id: str) -> Record:
        """Returns a stored record after fail-closed verification."""

    def purge(self, capsule_id: str) -> None:
        """Erases a record's present originals, keeping their commitments."""


@runtime_checkable
class TxStore(Store, Protocol):
    """The optional transactional extension, implemented by the SQL backends.
    Detect it with ``isinstance(store, TxStore)``. ``put_tx`` never commits or
    retries; on any error the caller rolls back the whole transaction."""

    def put_tx(self, tx: Any, record: Record) -> None:
        """Admits a record inside the caller's open transaction."""

    def get_tx(self, tx: Any, capsule_id: str, lock: bool = False) -> Record:
        """Reads inside the caller's open transaction, optionally locking."""


__all__ = [
    "AGENT_OUTPUT_DIGEST",
    "EFFECT_REQUEST_DIGEST",
    "EFFECT_RESPONSE_DIGEST",
    "NEVER_RETAINED",
    "PAYLOAD_DIGEST",
    "PRESENT",
    "PURGED",
    "Artifact",
    "ArtifactError",
    "ArtifactVerification",
    "ConflictError",
    "CorruptError",
    "DigestMismatchError",
    "InvalidError",
    "NotFoundError",
    "PurgedError",
    "Record",
    "Store",
    "TxStore",
    "UnsupportedError",
    "UntrustedSignerError",
    "prepare",
    "storage_checksum",
    "verify",
]
