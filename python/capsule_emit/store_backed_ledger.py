# SPDX-License-Identifier: Apache-2.0
"""A ledger kept in a CLL entry log and an artifact store.

The Python port of the Rust crate's store-backed ``ledger::Ledger``
(``Ledger::open_stores_with_indexer``, ``rust/capsule-emit/src/ledger.rs``):
the same ledger over the two standard layers, so a log written by either
language with both switches below on opens in the other. Rust has no
switches: a log holding a record admitted only with one off (an imported
flat ledger) opens only here, with the same switches.

- the CLL entry log (``cll.log.EntryStore``: jsonl, memory, sqlite or mysql)
  holds each line's 32-byte ``capsule_id``, in order;
- the artifact store (``capsule_emit.artifact``) holds each line as a record:
  the line without its inline ``signature``/``key_id``, as serde_json's
  compact JSON (member order kept, non-ASCII unescaped); the producer envelope
  those carry; and the signed statement as the unbound attachment
  :data:`STATEMENT`.

The same appends give byte-identical log values and records in both
languages. Opening replays the log with the checks the Rust ledger runs on
every line: the stored ``capsule_id`` recomputes (``agent_action_capsule``'s
JCS digest), a padding record has its shape, a chained record extends the
head, and the signed statement is a COSE_Sign1 whose payload names the line's
``capsule_id`` and, where the line carries ``key_id``, verifies under it.
Appends run the same checks before writing anything, so a refused line leaves
the ledger reopenable. A record is ``put`` first and its log entry appended
second, so the entry is the commit point: a crash between the two leaves a
record no entry names, and the retry's identical ``put`` is accepted.

Two switches exist for a log that holds a Python flat ledger's records
(:func:`import_flat`), each on by default:

- ``linear``: every chained record's parent is the head, Rust's rule. A
  Python flat ledger was never one chain (``seal()`` chains only on
  ``confirms=``, and a bilateral half confirms the other party's capsule), so
  ``linear=False`` drops the head-linkage rule;
- ``require_statements``: every record carries its signed statement. A Python
  flat ledger never wrote one, so ``require_statements=False`` admits a record
  with no :data:`STATEMENT` attachment at all. A purged statement is refused
  either way, and :meth:`Ledger.append` always takes one.

Checkpoint state lives on the same CLL backend: CLL's checkpoint runner
(``cll.checkpoint.runner.CheckpointRunner``), or the shelf's cadence for a host
log, checkpoints these entries. There is no checkpoint file.

**One writer.** A ``Ledger`` serializes its own appends, and each append first
checks that the log holds no entry this ``Ledger`` has not seen, so a second
writer fails with :class:`StaleLedgerError` instead of extending a head that
moved. That check narrows the race and does not close it: CLL ``append`` does
not compare parents, so cross-process exclusion is the caller's writer lock
(the shelf's, for a host log). Never purge a ledger record: purge erases the
signed statement, and the next open refuses the log.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_action_capsule.canonical import LOCAL_ONLY_FIELDS, json_digest
from cll.log import INSERTED, MAX_SCAN_LIMIT, AppendInput, CllError

from .artifact.record import (
    PADDING_RECORD_TYPE,
    PRESENT,
    Artifact,
    ArtifactError,
    NotFoundError,
    Record,
    _StrictJSONError,
    decode_strict_json,
    encode_record,
    prepare,
    verify,
)

__all__ = [
    "STATEMENT",
    "PADDING_CONTENT_TYPE",
    "CITATION_PURPOSE_COUNTERPARTY_HALF",
    "CITATION_PURPOSE_COUNTERPARTY_INCLUSION",
    "Ledger",
    "LedgerEntry",
    "LedgerLine",
    "RecoveryReport",
    "ImportReport",
    "LedgerError",
    "CapsuleIdMismatchError",
    "MissingCapsuleIdError",
    "ChainBrokenError",
    "StatementInvalidError",
    "AppendChainMismatchError",
    "MalformedPaddingError",
    "PaddingMisroutedError",
    "MissingRecordError",
    "StoreError",
    "LogError",
    "AlreadyInLogError",
    "EnvelopeError",
    "StaleLedgerError",
    "ImportRefusedError",
    "check_statement_matches",
    "compute_record_id",
    "counterparty_citation_keys",
    "counterparty_half_key",
    "counterparty_inclusion_key",
    "line_bytes",
    "padding_record_with_nonce",
    "import_flat",
]

#: The unbound artifact that holds a line's signed statement.
STATEMENT = "signed-statement"
#: The content type of a padding record's signed statement.
PADDING_CONTENT_TYPE = "application/json"
CITATION_PURPOSE_COUNTERPARTY_HALF = "counterparty_half"
CITATION_PURPOSE_COUNTERPARTY_INCLUSION = "counterparty_inclusion"

_ENVELOPE_MEMBERS = ("signature", "key_id")
_COSE_SIGN1_TAG = 18
#: The protected-header labels of a signed statement (alg, crit, content
#: type, kid, CWT claims): one may be marked critical, as Rust's verifier,
#: which never reads ``crit``, accepts.
_STATEMENT_LABELS = frozenset({1, 2, 3, 4, 15})
_PADDING_MEMBERS = frozenset({"capsule_id", "record_type", "epistemic_type", "store_nonce"})


# -- errors ----------------------------------------------------------------------
#
# One class per Rust ``LedgerError`` variant, plus the two this port adds
# (``StaleLedgerError`` and ``ImportRefusedError``). ``line`` is the 1-based
# log position the error is about (0 when it has none).


class LedgerError(Exception):
    """A ledger operation failed; nothing was written by the failing call
    unless its subclass says otherwise."""

    def __init__(self, message: str, *, line: int = 0, capsule_id: str = "") -> None:
        super().__init__(message)
        self.line = line
        self.capsule_id = capsule_id


class CapsuleIdMismatchError(LedgerError):
    """A line's stored ``capsule_id`` is not its recomputed digest, or a log
    entry names a record whose ``capsule_id`` differs."""


class MissingCapsuleIdError(LedgerError):
    """A line has no string ``capsule_id`` (or is not a JSON object)."""


class ChainBrokenError(LedgerError):
    """On open: a chained record whose parent is not the head (``linear``)."""


class StatementInvalidError(LedgerError):
    """A line's signed statement is missing, purged or does not check out."""


class AppendChainMismatchError(LedgerError):
    """On append: the capsule's parent is not the head (``linear``)."""


class MalformedPaddingError(LedgerError):
    """A padding record is not in a padding record's shape."""


class PaddingMisroutedError(LedgerError):
    """A padding record sent to ``append``, or a capsule to ``append_padding``."""


class MissingRecordError(LedgerError):
    """The log names a ``capsule_id`` the artifact store holds no record of."""


class StoreError(LedgerError):
    """The artifact store refused a read or a write; ``cause`` is its error."""

    def __init__(self, cause: ArtifactError, *, line: int = 0, capsule_id: str = "") -> None:
        super().__init__(f"artifact store: {cause.code}: {cause}", line=line, capsule_id=capsule_id)
        self.cause = cause


class LogError(LedgerError):
    """The CLL entry log failed; ``cause`` is its error."""

    def __init__(self, cause: CllError, *, line: int = 0, capsule_id: str = "") -> None:
        super().__init__(f"entry log: {cause}", line=line, capsule_id=capsule_id)
        self.cause = cause


class AlreadyInLogError(LedgerError):
    """The log already holds this ``capsule_id``, so appending would add no line."""


class EnvelopeError(LedgerError):
    """A line lacks a valid producer envelope whose signer ``key_id`` names."""


class StaleLedgerError(LedgerError):
    """The log holds entries this ``Ledger`` did not write or replay: another
    writer appended. Reopen under the writer lock."""


class ImportRefusedError(LedgerError):
    """A flat ledger holds lines no store record can keep; nothing was
    written. ``lines`` are their 1-based positions among the flat ledger's
    lines that parse (its log ``seq``)."""

    def __init__(self, message: str, lines: Sequence[int]) -> None:
        super().__init__(message)
        self.lines = list(lines)


# -- values ----------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """A record looked up by ``capsule_id``: the line and its signed statement
    (``None`` for an imported line that never had one)."""

    capsule: dict
    signed_statement: bytes | None


@dataclass(frozen=True)
class LedgerLine:
    """One log entry, padding included: line ``i`` is leaf ``i + 1``.
    ``offset`` is its log ``seq`` (the Rust flat layout's byte offset)."""

    capsule_id: str
    offset: int
    padding: bool
    timestamp: str | None


@dataclass
class RecoveryReport:
    """What :meth:`Ledger.open_stores` replayed. A torn tail is the backends'
    to repair when the caller provisions them, so
    ``truncated_torn_write_at`` is always ``None`` here (as in Rust)."""

    valid_entries: int = 0
    truncated_torn_write_at: int | None = None


@dataclass
class ImportReport:
    """What :func:`import_flat` did. ``records`` is the log's entry count
    after the import, ``resumed`` how many were already there (a re-run),
    ``skipped`` the flat lines not copied, by kind (checkpoint stamps, witness
    backfills, and repeats of a ``capsule_id`` already copied)."""

    records: int = 0
    resumed: int = 0
    skipped: dict = field(default_factory=dict)
    chain_head: str | None = None


# -- line form -------------------------------------------------------------------


def line_bytes(value: Any) -> bytes:
    """The bytes serde_json's ``to_vec`` writes for ``value`` with member order
    kept: compact, non-ASCII as UTF-8, only ``"``, ``\\`` and control
    characters escaped. A store-backed record's capsule bytes, and the payload
    of the signed statement a ledger line carries."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _is_padding(record: Any) -> bool:
    return isinstance(record, dict) and record.get("record_type") == PADDING_RECORD_TYPE


def _check_padding_shape(record: dict) -> str | None:
    """Why ``record`` is not a padding record's shape (Rust
    ``padding::check_padding_shape``), or ``None``."""
    for name in record:
        if name not in _PADDING_MEMBERS and name not in _ENVELOPE_MEMBERS:
            return f"padding record carries {name!r}; its only content is the store nonce"
    if record.get("record_type") != PADDING_RECORD_TYPE:
        return 'padding record\'s record_type is not "padding"'
    if record.get("epistemic_type") != "producer_claim":
        return 'padding record\'s epistemic_type is not "producer_claim"'
    nonce = record.get("store_nonce")
    if not (isinstance(nonce, str) and len(nonce) == 64 and all(c in "0123456789abcdef" for c in nonce)):
        return "padding record's store_nonce is not 64 lowercase hex"
    return None


def padding_record_with_nonce(store_nonce: str) -> dict:
    """The padding record for ``store_nonce``, ``capsule_id`` first, as Rust's
    ``padding_record_with_nonce`` builds it. A ledger pads with a fresh nonce
    (:meth:`Ledger.pad_to_bucket`); a nonce is never reused."""
    body = {
        "record_type": PADDING_RECORD_TYPE,
        "epistemic_type": "producer_claim",
        "store_nonce": store_nonce,
    }
    return {"capsule_id": json_digest(body), **body}


def compute_record_id(record: dict) -> str:
    """The digest primitive Rust's ``jcs::compute_capsule_id`` is: the
    JSON-DIGEST of ``record`` without ``capsule_id``, ``signature`` and
    ``key_id``. Unlike ``agent_action_capsule.canonical.compute_capsule_id``
    it checks neither ``format_version`` nor ``canonicalization_id`` (a
    verifier's job), so it also names padding records."""
    excluded = {"capsule_id", *LOCAL_ONLY_FIELDS}
    return json_digest({k: v for k, v in record.items() if k not in excluded})


def _recompute(line_no: int, record: dict, stored: str) -> None:
    try:
        recomputed = compute_record_id(record)
    except (ValueError, TypeError) as exc:
        raise CapsuleIdMismatchError(
            f"ledger line {line_no}: capsule_id does not recompute: {exc}", line=line_no, capsule_id=stored
        ) from exc
    if recomputed != stored:
        raise CapsuleIdMismatchError(
            f"ledger line {line_no} corrupt: stored capsule_id {stored!r} does not match recomputed digest "
            f"{recomputed!r} -- refusing to trust this entry",
            line=line_no,
            capsule_id=stored,
        )


def _public_pem(raw: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )


def check_statement_matches(capsule: dict, capsule_id: str, statement: bytes) -> str | None:
    """Why ``statement`` is not ``capsule``'s signed statement, or ``None``
    (Rust ``check_statement_matches``): a COSE_Sign1 whose payload is JSON
    naming ``capsule_id``, verified under the line's ``key_id`` when it
    carries one."""
    from scitt_cose.cose_sign1 import strict_decode, verify_sign1

    key_hex = capsule.get("key_id")
    if key_hex is not None:
        try:
            raw = bytes.fromhex(key_hex) if isinstance(key_hex, str) else b""
            pem = _public_pem(raw)
        except ValueError:
            return f"capsule key_id {key_hex!r} is not a valid Ed25519 public key"
        try:
            payload = verify_sign1(
                bytes(statement), public_key_pem=pem, understood_labels=_STATEMENT_LABELS
            ).payload
        except Exception as exc:  # scitt_cose raises several types for a bad statement
            return f"COSE verification against the capsule's own key_id failed: {exc}"
    else:
        try:
            message = strict_decode(bytes(statement))
            if (
                message.tag != _COSE_SIGN1_TAG
                or not isinstance(message.value, (list, tuple))
                or len(message.value) != 4
            ):
                return "not a parseable COSE_Sign1: not a tag-18 array of four"
            payload = message.value[2]
        except Exception as exc:
            return f"not a parseable COSE_Sign1: {exc}"
    if not isinstance(payload, (bytes, bytearray)):
        return "message has no attached payload"
    try:
        named = decode_strict_json(bytes(payload))
    except _StrictJSONError as exc:
        return f"COSE payload is not valid JSON: {exc}"
    got = named.get("capsule_id") if isinstance(named, dict) else None
    if got != capsule_id:
        return f"COSE payload names capsule_id {got!r}, expected {capsule_id!r}"
    return None


def _check_statement(line_no: int, record: dict, capsule_id: str, statement: bytes) -> None:
    detail = check_statement_matches(record, capsule_id, statement)
    if detail is not None:
        raise StatementInvalidError(
            f"ledger line {line_no} corrupt: signed statement for capsule_id {capsule_id} does not check out "
            f"({detail}) -- refusing to trust this entry",
            line=line_no,
            capsule_id=capsule_id,
        )


def _statement_missing(line_no: int, capsule_id: str) -> StatementInvalidError:
    return StatementInvalidError(
        f"ledger line {line_no}: the record's {STATEMENT} attachment is missing or purged",
        line=line_no,
        capsule_id=capsule_id,
    )


def _to_record(capsule_id: str, line: dict, statement: bytes) -> Record:
    """A ledger line as an artifact record (Rust ``to_record``): refuses a line
    without a valid envelope, or whose ``key_id`` is not exactly its signer
    in lowercase hex, so the record always reads back as the line."""
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    def refuse(detail: str) -> EnvelopeError:
        return EnvelopeError(f"capsule_id {capsule_id}: producer envelope: {detail}", capsule_id=capsule_id)

    if not isinstance(line, dict):
        raise refuse("the line is not a JSON object")
    body = dict(line)
    signature = body.pop("signature", None)
    key_id = body.pop("key_id", None)
    if not isinstance(signature, str) or not isinstance(key_id, str):
        raise refuse("a store-backed ledger needs the line's producer envelope (signature and key_id)")
    try:
        envelope = bytes.fromhex(signature)
    except ValueError:
        raise refuse("signature is not hex") from None
    verified = verify_producer_envelope(capsule_id, envelope)
    if not verified.ok or verified.public_key is None:
        raise refuse("the envelope does not verify over the capsule_id")
    if key_id != verified.public_key.hex():
        raise refuse("key_id is not the envelope's signer")
    if signature != envelope.hex():
        raise refuse("signature is not lowercase hex")
    content = bytes(statement)
    return Record(
        capsule_id,
        line_bytes(body),
        envelope,
        [Artifact(STATEMENT, PRESENT, "", content, hashlib.sha256(content).hexdigest())],
    )


# -- indexes ---------------------------------------------------------------------

RecordIndexer = Callable[[dict], Iterable[str]]


def counterparty_half_key(foreign_capsule_id: str) -> str:
    """Index key for a record citing another party's record of the same exchange."""
    return f"{CITATION_PURPOSE_COUNTERPARTY_HALF}:{foreign_capsule_id}"


def counterparty_inclusion_key(foreign_capsule_id: str) -> str:
    """Index key for a record citing another party's inclusion proof for its
    record ``foreign_capsule_id``."""
    return f"{CITATION_PURPOSE_COUNTERPARTY_INCLUSION}:{foreign_capsule_id}"


def counterparty_citation_keys(capsule: dict) -> list:
    """The default indexer (Rust ``counterparty_citation_keys``): a
    ``counterparty_half`` citation's key names the cited capsule; a
    ``counterparty_inclusion`` citation's key names
    ``compute_attestation.counterparty_inclusion.half_capsule_id``."""
    keys: list = []
    references = capsule.get("references")
    if not isinstance(references, list):
        return keys
    purposes = [r.get("citation_purpose") if isinstance(r, dict) else None for r in references]
    for reference, purpose in zip(references, purposes):
        digest = reference.get("digest") if isinstance(reference, dict) else None
        if purpose == CITATION_PURPOSE_COUNTERPARTY_HALF and isinstance(digest, str):
            keys.append(counterparty_half_key(digest))
    if CITATION_PURPOSE_COUNTERPARTY_INCLUSION in purposes:
        half: Any = capsule
        for name in ("model_attestation", "compute_attestation", "counterparty_inclusion", "half_capsule_id"):
            half = half.get(name) if isinstance(half, dict) else None
        if isinstance(half, str):
            keys.append(counterparty_inclusion_key(half))
    return keys


# -- the ledger --------------------------------------------------------------------


def _parent(record: dict) -> str | None:
    chain = record.get("chain")
    parent = chain.get("parent_capsule_id") if isinstance(chain, dict) else None
    return parent if isinstance(parent, str) else None


def _timestamp(record: dict) -> str | None:
    value = record.get("timestamp")
    return value if isinstance(value, str) else None


class Ledger:
    """A ledger over a CLL entry log and an artifact store. Open it with
    :meth:`open_stores`; see the module docstring for the guarantees."""

    def __init__(
        self, log: Any, artifacts: Any, indexer: RecordIndexer, linear: bool, require_statements: bool
    ):
        self._log = log
        self._artifacts = artifacts
        self._indexer = indexer
        self._linear = linear
        self._require_statements = require_statements
        self._index: dict[str, int] = {}
        self._index_keys: set[str] = set()
        self._lines: list[LedgerLine] = []
        self._chain_head: str | None = None
        # Reentrant: pad_to_bucket holds it across its count and its appends.
        self._lock = threading.RLock()

    @classmethod
    def open_stores(
        cls,
        log: Any,
        artifacts: Any,
        indexer: RecordIndexer = counterparty_citation_keys,
        *,
        linear: bool = True,
        require_statements: bool = True,
    ) -> tuple[Ledger, RecoveryReport]:
        """Open the ledger ``log`` and ``artifacts`` hold, replaying every
        entry with the checks :meth:`append` runs. Both stores must already be
        provisioned (``init``); an empty log is an empty ledger. The artifact
        store's trusted keys must include every key that signed a line. A
        deployment that indexes its own record kinds usually calls
        :func:`counterparty_citation_keys` from its ``indexer``."""
        ledger = cls(log, artifacts, indexer, linear, require_statements)
        report = RecoveryReport()
        for entry in ledger._scan(0):
            line_no = entry.seq
            capsule_id = bytes(entry.value).hex()
            parsed, statement, attached = ledger._read(line_no, capsule_id)
            if parsed.get("capsule_id") != capsule_id:
                stored = parsed.get("capsule_id")
                raise CapsuleIdMismatchError(
                    f"ledger line {line_no} corrupt: the log names {capsule_id} but its record carries {stored!r}",
                    line=line_no,
                    capsule_id=capsule_id,
                )
            ledger._admit(line_no, entry.seq, parsed, statement, attached)
            report.valid_entries += 1
        return ledger, report

    @classmethod
    def open_stores_with_indexer(
        cls, log: Any, artifacts: Any, indexer: RecordIndexer, **switches: bool
    ) -> tuple[Ledger, RecoveryReport]:
        """:meth:`open_stores` with a caller-supplied indexer, the Rust name."""
        return cls.open_stores(log, artifacts, indexer, **switches)

    # -- reads --------------------------------------------------------------------

    def _scan(self, after: int):
        while True:
            try:
                page = self._log.scan_entries(after, MAX_SCAN_LIMIT)
            except CllError as exc:
                raise LogError(exc, line=after + 1) from exc
            if not page:
                return
            for entry in page:
                after = entry.seq
                yield entry

    def _read(self, line_no: int, capsule_id: str) -> tuple[dict, bytes | None, bool]:
        """The record for ``capsule_id`` as a ledger line (``signature`` and
        ``key_id`` restored from its envelope), its statement (``None`` when
        missing or purged) and whether it has a :data:`STATEMENT` attachment."""
        from agent_action_capsule.producer_envelope import verify_producer_envelope

        try:
            record = self._artifacts.get(capsule_id)
        except NotFoundError:
            raise MissingRecordError(
                f"ledger line {line_no}: the log names capsule_id {capsule_id} but the artifact store has "
                "no record of it",
                line=line_no,
                capsule_id=capsule_id,
            ) from None
        except ArtifactError as exc:
            raise StoreError(exc, line=line_no, capsule_id=capsule_id) from exc
        try:
            parsed = decode_strict_json(record.capsule)
        except _StrictJSONError as exc:
            raise MissingCapsuleIdError(
                f"ledger line {line_no}: record JSON error: {exc}", line=line_no, capsule_id=capsule_id
            ) from exc
        if not isinstance(parsed, dict):
            raise MissingCapsuleIdError(
                f"ledger line {line_no} corrupt: the record is not a JSON object",
                line=line_no,
                capsule_id=capsule_id,
            )
        envelope = bytes(record.producer_envelope)
        verified = verify_producer_envelope(capsule_id, envelope)
        parsed["signature"] = envelope.hex()
        parsed["key_id"] = verified.public_key.hex() if verified.public_key is not None else ""
        statements = [a for a in record.artifacts if a.name == STATEMENT and not a.binding]
        statement = statements[0].content if statements else None
        return parsed, (bytes(statement) if statement is not None else None), bool(statements)

    @property
    def entries(self) -> int:
        """Every line in the ledger, padding included."""
        return len(self._lines)

    @property
    def chain_head(self) -> str | None:
        """The last record appended (padding never is)."""
        return self._chain_head

    def contains(self, capsule_id: str) -> bool:
        return capsule_id in self._index

    def known_capsule_ids(self) -> set:
        """Every indexed ``capsule_id``, padding excluded."""
        return set(self._index)

    def capsule_ids_in_order(self) -> list:
        """Every indexed ``capsule_id`` in log order."""
        return [cid for cid, _ in sorted(self._index.items(), key=lambda item: item[1])]

    def __len__(self) -> int:
        return len(self._index)

    def has_index_key(self, key: str) -> bool:
        """Whether the indexer produced ``key`` for any chained record."""
        return key in self._index_keys

    def cites_counterparty_half(self, foreign_capsule_id: str) -> bool:
        """Whether a record cites the other party's record ``foreign_capsule_id``
        (a dedup gate; a custom indexer must include the default keys)."""
        return self.has_index_key(counterparty_half_key(foreign_capsule_id))

    def cites_counterparty_inclusion(self, foreign_capsule_id: str) -> bool:
        return self.has_index_key(counterparty_inclusion_key(foreign_capsule_id))

    def lines(self) -> list:
        """Every line in log order, padding included."""
        return list(self._lines)

    def read_line(self, offset: int) -> dict:
        """The JSON of the line at log ``seq`` ``offset`` (from :meth:`lines`)."""
        if not 1 <= offset <= len(self._lines):
            raise LogError(CllError(f"no log entry at seq {offset}"), line=offset)
        return self._read(offset, self._lines[offset - 1].capsule_id)[0]

    def read_statement(self, capsule_id: str) -> bytes | None:
        """The signed statement stored for ``capsule_id``, padding included;
        ``None`` for an imported record that never had one."""
        line_no = self._line_number(capsule_id)
        _, statement, attached = self._read(line_no, capsule_id)
        if attached and statement is None:
            raise _statement_missing(line_no, capsule_id)
        return statement

    def lookup(self, capsule_id: str) -> LedgerEntry | None:
        """The record and its statement by ``capsule_id``, ``None`` when the
        ledger holds no such record (padding is not looked up)."""
        seq = self._index.get(capsule_id)
        if seq is None:
            return None
        capsule, statement, attached = self._read(seq, capsule_id)
        if attached and statement is None:
            raise _statement_missing(seq, capsule_id)
        return LedgerEntry(capsule, statement)

    def _line_number(self, capsule_id: str) -> int:
        for i, line in enumerate(self._lines):
            if line.capsule_id == capsule_id:
                return i + 1
        return 0

    # -- replay and writes ----------------------------------------------------------

    def _admit(self, line_no: int, seq: int, parsed: dict, statement: bytes | None, attached: bool) -> None:
        """Checks line ``line_no`` as Rust's ``Replay::admit`` does and indexes it."""
        stored = parsed.get("capsule_id")
        if not isinstance(stored, str):
            raise MissingCapsuleIdError(
                f"ledger line {line_no} corrupt: missing or non-string capsule_id field", line=line_no
            )
        _recompute(line_no, parsed, stored)
        padding = _is_padding(parsed)
        if padding:
            detail = _check_padding_shape(parsed)
            if detail is not None:
                raise MalformedPaddingError(
                    f"ledger line {line_no}: malformed padding record: {detail}", line=line_no
                )
        parent = _parent(parsed)
        if not padding and self._linear and parent != self._chain_head:
            raise ChainBrokenError(
                f"ledger line {line_no} references chain parent {parent!r} but the ledger head at that point was "
                f"{self._chain_head!r} -- chain is broken",
                line=line_no,
                capsule_id=stored,
            )
        if statement is not None:
            _check_statement(line_no, parsed, stored, statement)
        elif attached or self._require_statements:
            raise _statement_missing(line_no, stored)
        self._index_line(stored, seq, parsed, padding)

    def _index_line(self, capsule_id: str, seq: int, record: dict, padding: bool) -> None:
        self._lines.append(LedgerLine(capsule_id, seq, padding, _timestamp(record)))
        if padding:
            return  # outside the chain: never the head, never indexed
        self._index_keys.update(self._indexer(record))
        self._index[capsule_id] = seq
        self._chain_head = capsule_id

    def _check_not_stale(self) -> None:
        try:
            ahead = self._log.scan_entries(len(self._lines), 1)
        except CllError as exc:
            raise LogError(exc) from exc
        if ahead:
            raise StaleLedgerError(
                f"the log holds entry {ahead[0].seq}, past the {len(self._lines)} this ledger has: another writer "
                "appended; reopen under the writer lock"
            )

    def _commit(self, capsule_id: str, record: Record, line: dict, padding: bool) -> int:
        """Writes one line: its record, then its log entry (the commit point).
        Caller holds ``_lock`` and has checked the line."""
        try:
            self._artifacts.put(record)
        except ArtifactError as exc:
            raise StoreError(exc, line=len(self._lines) + 1, capsule_id=capsule_id) from exc
        try:
            appended = self._log.append(AppendInput(bytes.fromhex(capsule_id), datetime.now(timezone.utc)))
        except CllError as exc:
            raise LogError(exc, line=len(self._lines) + 1, capsule_id=capsule_id) from exc
        if appended.outcome != INSERTED:
            raise AlreadyInLogError(
                f"refusing to append: capsule_id {capsule_id} is already in the log", capsule_id=capsule_id
            )
        self._index_line(capsule_id, appended.entry.seq, line, padding)
        return appended.entry.seq

    def _write_line(self, capsule_id: str, line: dict, statement: bytes, padding: bool) -> int:
        line_no = len(self._lines) + 1
        _recompute(line_no, line, capsule_id)
        _check_statement(line_no, line, capsule_id, statement)
        record = _to_record(capsule_id, line, statement)
        self._check_not_stale()
        return self._commit(capsule_id, record, line, padding)

    def append(self, capsule: dict, signed_statement: bytes) -> None:
        """Append a sealed capsule (with its inline producer envelope) and its
        signed statement. Refuses a padding record, a capsule whose parent is
        not the head (``linear``), and any line a reopen would refuse."""
        if _is_padding(capsule):
            raise PaddingMisroutedError("refusing to append: a padding record goes through append_padding")
        capsule_id = capsule.get("capsule_id")
        if not isinstance(capsule_id, str):
            raise MissingCapsuleIdError("capsule has no capsule_id field")
        with self._lock:
            parent = _parent(capsule)
            if self._linear and parent != self._chain_head:
                raise AppendChainMismatchError(
                    f"refusing to append: capsule's chain.parent_capsule_id {parent!r} does not match ledger head "
                    f"{self._chain_head!r}",
                    capsule_id=capsule_id,
                )
            self._write_line(capsule_id, capsule, signed_statement, False)

    def append_record(self, record: Record) -> tuple[int, bool]:
        """Append a sealed record with the originals it carries, which the
        artifact store retains as given (a Result's ``payload`` document, for
        one): returns its 1-based position and whether this call wrote it
        (``False``: the ledger already held this record). The capsule is
        stored in the ledger's line form; the producer envelope must verify
        over its id and the id must recompute. A record with no
        :data:`STATEMENT` attachment needs ``require_statements=False``; one
        with it is checked as :meth:`append` checks it. A record that is not
        the head's child is refused unless the ledger is ``linear=False``.
        Refuses a padding record and a different record under a held id."""
        from agent_action_capsule.producer_envelope import verify_producer_envelope

        capsule_id = record.capsule_id
        try:
            parsed = decode_strict_json(bytes(record.capsule))
        except _StrictJSONError as exc:
            raise MissingCapsuleIdError(f"record JSON error: {exc}", capsule_id=capsule_id) from exc
        if not isinstance(parsed, dict) or parsed.get("capsule_id") != capsule_id:
            raise MissingCapsuleIdError("the capsule has no capsule_id naming the record", capsule_id=capsule_id)
        if _is_padding(parsed):
            raise PaddingMisroutedError("refusing to append: a padding record goes through append_padding")
        if any(name in parsed for name in _ENVELOPE_MEMBERS):
            raise EnvelopeError(
                f"capsule_id {capsule_id}: signature and key_id are not capsule members", capsule_id=capsule_id
            )
        verified = verify_producer_envelope(capsule_id, bytes(record.producer_envelope))
        if not verified.ok or verified.public_key is None:
            raise EnvelopeError(
                f"capsule_id {capsule_id}: producer envelope: the envelope does not verify over the capsule_id",
                capsule_id=capsule_id,
            )
        stored = prepare(
            Record(capsule_id, line_bytes(parsed), bytes(record.producer_envelope), list(record.artifacts))
        )
        with self._lock:
            line_no = len(self._lines) + 1
            _recompute(line_no, parsed, capsule_id)
            if self.contains(capsule_id):
                held = self._get_record(capsule_id, line_no)
                if (held.capsule, held.producer_envelope) != (stored.capsule, stored.producer_envelope) or [
                    (a.name, a.state, a.binding, a.content) for a in held.artifacts
                ] != [(a.name, a.state, a.binding, a.content) for a in stored.artifacts]:
                    raise AlreadyInLogError(
                        f"capsule_id {capsule_id} is already in the ledger as a different record",
                        capsule_id=capsule_id,
                    )
                return self._index[capsule_id], False
            parent = _parent(parsed)
            if self._linear and parent != self._chain_head:
                raise AppendChainMismatchError(
                    f"refusing to append: capsule's chain.parent_capsule_id {parent!r} does not match ledger head "
                    f"{self._chain_head!r}",
                    capsule_id=capsule_id,
                )
            statements = [a for a in stored.artifacts if a.name == STATEMENT and not a.binding]
            if statements and statements[0].content is not None:
                _check_statement(line_no, {**parsed, "key_id": verified.public_key.hex()}, capsule_id, statements[0].content)
            elif self._require_statements:
                raise _statement_missing(line_no, capsule_id)
            self._check_not_stale()
            line = {**parsed, "signature": bytes(record.producer_envelope).hex(), "key_id": verified.public_key.hex()}
            return self._commit(capsule_id, stored, line, False), True

    def _get_record(self, capsule_id: str, line_no: int) -> Record:
        try:
            return self._artifacts.get(capsule_id)
        except ArtifactError as exc:
            raise StoreError(exc, line=line_no, capsule_id=capsule_id) from exc

    def append_padding(self, record: dict, signed_statement: bytes) -> None:
        """Append one padding record and its signed statement. The head does
        not move and the record is not indexed."""
        if not _is_padding(record):
            raise PaddingMisroutedError("refusing to append: append_padding only takes a padding record")
        with self._lock:
            detail = _check_padding_shape(record)
            if detail is not None:
                raise MalformedPaddingError(
                    f"ledger line {len(self._lines) + 1}: malformed padding record: {detail}",
                    line=len(self._lines) + 1,
                )
            capsule_id = record.get("capsule_id")
            if not isinstance(capsule_id, str):
                raise MissingCapsuleIdError("capsule has no capsule_id field")
            self._write_line(capsule_id, record, signed_statement, True)

    def pad_to_bucket(self, bucket: int, signer: Any, issuer: str) -> int:
        """Append padding records until the line count is a multiple of
        ``bucket`` (a no-op when it is), each with a fresh nonce, the producer
        envelope of ``signer`` and a signed statement from it. ``signer`` is a
        ``capsule_emit.signing`` signer with ``sign_envelope`` and
        ``sign_cose_statement`` (such as ``LocalKeypairSigner``). Returns the
        line count. No append from another thread lands between the padding
        records; across processes, the caller holds the writer lock."""
        from .signing import sign_producer_envelope

        with self._lock:
            return self._pad_to_bucket(bucket, signer, issuer, sign_producer_envelope)

    def _pad_to_bucket(self, bucket: int, signer: Any, issuer: str, sign_producer_envelope: Callable) -> int:
        if bucket <= 0:
            return self.entries
        missing = (bucket - self.entries % bucket) % bucket
        for _ in range(missing):
            record = padding_record_with_nonce(secrets.token_hex(32))
            record["signature"], record["key_id"] = sign_producer_envelope(signer, record["capsule_id"])
            statement = signer.sign_cose_statement(
                line_bytes(record),
                content_type=PADDING_CONTENT_TYPE,
                issuer=issuer,
                subject=record["capsule_id"],
            )
            self.append_padding(record, statement)
        return self.entries


# -- importing a flat Python ledger ------------------------------------------------


def import_flat(
    source: str | os.PathLike,
    log: Any,
    artifacts: Any,
    indexer: RecordIndexer = counterparty_citation_keys,
) -> tuple[Ledger, ImportReport]:
    """Copy the records of the Python flat ledger at ``source`` (a
    ``ledger.jsonl`` or a ledger store directory) into ``log`` and
    ``artifacts``, in order, each ``put`` exactly as
    ``capsule_emit.legacy_ledger.LegacyLedger.get`` returns it and appended.
    Nothing is re-signed. Returns the ledger, opened with ``linear=False`` and
    ``require_statements=False`` (the flat ledger's rules), and a report.

    Checkpoint stamps and witness backfills are the flat ledger's checkpoint
    machinery, not records, and are not copied; checkpoint the new log on its
    CLL backend, and keep the flat file, which still holds them. A repeated
    ``capsule_id`` (an old writer's identical duplicate line) is copied once;
    a different line under the same ``capsule_id`` refuses. Any other
    line no store record can keep, such as an unsigned ``log()`` entry, a
    disclosure record or a capsule failing Class 1, refuses the whole import
    with :class:`ImportRefusedError` before anything is written.

    Re-runnable: the log is the journal. An entry already in the log must
    hold the byte-identical record (a resumed import); a record ``put`` whose
    entry a crash never appended is ``put`` again, which the store accepts.
    The caller holds the writer lock of both the flat ledger and the stores.

    Every record is ``put`` before any entry is appended, so a record the
    store refuses, such as one from a signer it does not trust, raises
    :class:`StoreError` while the log holds no new entry (records already
    ``put`` are named by no entry, so they are not ledger lines); re-run once
    the store trusts the key."""
    from . import ledger as _ledger
    from .legacy_ledger import LegacyLedger

    if not os.path.exists(source):
        raise FileNotFoundError(f"no flat ledger at {os.fspath(source)}")
    legacy = LegacyLedger(source)
    copy, skipped, refused = [], {}, []
    seen: dict[str, dict] = {}
    for number, obj in enumerate(_ledger.read_ledger_entries(os.fspath(source)), start=1):
        if not isinstance(obj, dict):
            refused.append((number, "a line that is not a JSON object"))
            continue
        kind = obj.get("kind")
        if kind in (_ledger.CHECKPOINT_STAMP_KIND, _ledger.WITNESS_BACKFILL_KIND):
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        capsule_id = obj.get("capsule_id")
        if kind is not None or not isinstance(capsule_id, str):
            refused.append((number, f"a {kind or 'non-record'} line"))
            continue
        if capsule_id in seen:
            if obj != seen[capsule_id]:
                refused.append((number, f"a second, different line for capsule_id {capsule_id}"))
            else:
                skipped["repeat"] = skipped.get("repeat", 0) + 1
            continue
        try:
            record = legacy.get(capsule_id)
            if not record.producer_envelope:
                raise ArtifactError("an unsigned line has no producer envelope")
            prepared = prepare(record)
            verify(prepared, [_signer_key(record)])
        except ArtifactError as exc:
            refused.append((number, str(exc)))
            continue
        seen[capsule_id] = obj
        copy.append((number, record))
    if refused:
        shown = "; ".join(f"line {n}: {why}" for n, why in refused[:5])
        raise ImportRefusedError(
            f"{len(refused)} line(s) of {os.fspath(source)} cannot be store records, nothing was written: {shown}",
            [n for n, _ in refused],
        )

    ledger, _ = Ledger.open_stores(log, artifacts, indexer, linear=False, require_statements=False)
    report = ImportReport(skipped=skipped)
    with ledger._lock:
        if ledger.entries > len(copy):
            raise ImportRefusedError(
                f"the log holds {ledger.entries} entries, the flat ledger {len(copy)} records: refusing to resume",
                [],
            )
        # A resumed import: each entry already in the log holds its record as stored.
        for position, (number, record) in enumerate(copy[: ledger.entries], start=1):
            held = ledger._lines[position - 1].capsule_id
            if held != record.capsule_id or encode_record(_get(artifacts, held)) != encode_record(
                prepare(record)
            ):
                raise ImportRefusedError(
                    f"the log's entry {position} is not flat line {number}'s record as stored: refusing to resume",
                    [number],
                )
            report.resumed += 1
        rest = copy[ledger.entries :]
        # Every record first, then every entry: a record the store refuses
        # (an untrusted signer) stops the import before the log holds any new
        # entry, and a record no entry names is not a ledger line.
        for number, record in rest:
            try:
                artifacts.put(record)
            except ArtifactError as exc:
                raise StoreError(exc, line=number, capsule_id=record.capsule_id) from exc
        for _, record in rest:
            line = decode_strict_json(record.capsule)
            ledger._check_not_stale()
            ledger._commit(record.capsule_id, record, line, _is_padding(line))
    if ledger.entries != len(copy):
        raise ImportRefusedError(
            f"the log holds {ledger.entries} entries after the import, the flat ledger {len(copy)} records",
            [],
        )
    # Reopen from the stores: every copied record passes the replay checks.
    reopened, _ = Ledger.open_stores(log, artifacts, indexer, linear=False, require_statements=False)
    report.records = reopened.entries
    report.chain_head = reopened.chain_head
    return reopened, report


def _signer_key(record: Record) -> bytes:
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    verified = verify_producer_envelope(record.capsule_id, bytes(record.producer_envelope))
    return verified.public_key or b""


def _get(artifacts: Any, capsule_id: str) -> Record:
    try:
        return artifacts.get(capsule_id)
    except ArtifactError as exc:
        raise StoreError(exc, capsule_id=capsule_id) from exc
