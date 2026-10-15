# SPDX-License-Identifier: Apache-2.0
"""The flat ``ledger.jsonl`` as a backend for both storage interfaces.

``LegacyLedger`` puts the ledger file that ``seal()`` has always written behind
the CLL entry log (``cll.log.EntryStore``, checkpointed-local-log's
``docs/backend-contract.md``) and the artifact store
(``capsule_emit.artifact.Store``, ``docs/artifact-store.md``). Existing ledgers
open unchanged, keep their checkpoint roots, and new capsules produce the same
lines as before. See ``docs/legacy-ledger.md`` for the full mapping.

CLL entry log
-------------

Every line is a leaf: capsules, unsigned log entries, checkpoint stamps,
disclosure records, witness backfills and padding. ``Entry.seq`` is the line's
1-based position among the lines that parse (blank and unparseable lines are
skipped, as ``read_ledger_entries`` and the checkpoint index always have), and
``Entry.value`` is its ``capsule_id`` as 32 bytes. A line whose ``capsule_id``
is not 64 hex digits is ``corrupt``.

``Entry.appended_at`` is derived, because the ledger stores no append time:
the line's top-level ``timestamp`` (capsules, log entries, disclosure records),
else its ``checkpoint.timestamp`` (checkpoint stamps), else the previous
entry's ``appended_at`` (the Unix epoch for the first line). The time is read
as an RFC 3339 instant (no offset means UTC) and truncated to milliseconds.

The one contract exception: the ledger has never deduplicated, so a historical
file can repeat a ``capsule_id``. ``scan_entries`` returns every line,
duplicates included, so sequence positions and roots never change;
``get_entry`` returns the first occurrence. New writes are idempotent.

Writes
------

A ledger line is a whole record, never a bare digest. A capsule is written by
artifact ``put``, which appends its line and so its log entry. ``append``
lacks the ``digest-append`` capability: it returns the existing entry as
``idempotent`` for a digest the ledger holds and raises ``unsupported``
otherwise. Checkpoint stamps, disclosure records and witness backfills keep
their writer, ``capsule_emit.ledger.append_to_ledger``, under the same
in-process lock and cross-process ``flock``.

Artifact store
--------------

A capsule line carries the Producer Envelope inline: ``signature`` (hex
COSE_Sign1) and ``key_id`` (the signer's raw Ed25519 public key, hex). ``get``
returns the line minus those two members as compact JSON, and the envelope
bytes. Lines with neither member (``log()`` entries, capsules sealed before
producer envelopes) come back with an empty ``producer_envelope``. Checkpoint
stamps, disclosure records, witness backfills and padding are log entries
only: ``get`` reports them ``not_found``.

The capsule is checked with capsule-emit's own rules, which keep both
canonicalization profiles: ``capsule_id`` is recomputed per
``canonicalization_id`` (``jcs``, or ``jcs-n`` when it is absent), and a
present envelope must verify over that ``capsule_id`` with ``key_id`` as its
signer. With ``trusted`` keys the signer must also be one of them
(``untrusted_signer``); without them, any valid signer is accepted, as
``seal()`` itself accepts any configured key. Class 1 structure checks stay with
``capsule-emit verify``.

Capability limits (``CAPABILITIES``), each refused with ``unsupported``:

- no originals: ``put`` of a record that declares any artifact;
- no purge: ``purge`` after validating the ID;
- no digest-only append (above);
- single writer, no transactions: no ``TxStore`` extension.

``put`` also refuses (``invalid``) capsule bytes that are not the ledger's own
line form, compact ``json.dumps`` output with ASCII escapes, because the file
cannot hold other bytes for the same JSON value.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cll.log import IDEMPOTENT, INSERTED, AppendInput, AppendResult, ClosedError, Entry
from cll.log import CorruptError as LogCorruptError
from cll.log import NotFoundError as LogNotFoundError
from cll.log import UnsupportedError as LogUnsupportedError
from cll.log.engine import check_append, check_scan, check_value
from cll.log.ledger import capsule_timestamp

from . import ledger as _ledger
from .artifact.record import (
    ConflictError,
    InvalidError,
    NotFoundError,
    Record,
    UnsupportedError,
    UntrustedSignerError,
    _StrictJSONError,
    check_store_config,
    decode_strict_json,
    prepare,
    valid_id,
    valid_name,
)
from .canonicalization import UnsupportedCanonicalizationError, compute_capsule_id

__all__ = ["CAPABILITIES", "DEFAULT_NAMESPACE", "LegacyLedger", "shared"]

#: What the legacy backend offers, by capability name. Every ``False`` is
#: refused with ``unsupported`` (or, for the last two, simply not offered).
CAPABILITIES = {
    "digest-append": False,
    "originals": False,
    "purge": False,
    "transactions": False,
    "multiple-writers": False,
}

#: The namespace a ``LegacyLedger`` reports when none is given. A ledger file
#: holds one namespace, like an artifact ``jsonl`` file.
DEFAULT_NAMESPACE = "ledger"

_ENVELOPE_MEMBERS = ("signature", "key_id")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Line:
    entry: Entry
    offset: int
    length: int
    capsule: bool


class _Stamped:
    """Shapes a ledger line for ``cll.log.ledger.capsule_timestamp``."""

    def __init__(self, seq: int, timestamp: Any) -> None:
        self.seq = seq
        self.capsule = {"timestamp": timestamp}


def _line_time(seq: int, obj: dict) -> datetime | None:
    timestamp = obj.get("timestamp")
    if timestamp is None and isinstance(obj.get("checkpoint"), dict):
        timestamp = obj["checkpoint"].get("timestamp")
    if timestamp is None:
        return None
    try:
        return capsule_timestamp(_Stamped(seq, timestamp))
    except LogCorruptError:
        return None


def _is_capsule(obj: dict) -> bool:
    return obj.get("kind") not in _ledger.NON_CAPSULE_KINDS and obj.get("record_type") != _ledger.PADDING_RECORD_TYPE


def _line_form(value: dict) -> str:
    """The exact text ``append_to_ledger`` writes for ``value``."""
    return json.dumps(value, separators=(",", ":"))


def _verify_capsule(capsule_id: str, capsule: dict, envelope: bytes, trusted: list | None) -> str | None:
    """capsule-emit's legacy check. Returns the signer ``key_id`` (hex), or
    ``None`` for an unsigned capsule."""
    if capsule.get("capsule_id") != capsule_id:
        raise InvalidError("capsule verification: carried capsule_id differs")
    try:
        computed = compute_capsule_id(capsule)
    except (UnsupportedCanonicalizationError, ValueError, TypeError) as exc:
        raise InvalidError(f"capsule verification: {exc}") from exc
    if computed != capsule_id:
        raise InvalidError("capsule verification: capsule id mismatch")
    if not envelope:
        return None
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    result = verify_producer_envelope(capsule_id, bytes(envelope))
    if not result.ok or result.public_key is None:
        raise InvalidError("envelope: " + ",".join(f.code for f in result.findings))
    if trusted is not None and result.public_key not in trusted:
        raise UntrustedSignerError("producer signer is not trusted")
    return result.public_key.hex()


class LegacyLedger:
    """A ``ledger.jsonl`` file as a CLL ``EntryStore`` and an artifact
    ``Store``, with the capability limits in ``CAPABILITIES``.

    ``trusted`` (raw 32-byte Ed25519 public keys) turns on signer
    authorization; ``None`` accepts any signer whose envelope verifies.
    ``wait``/``timeout`` are passed to the writer lock, as for
    ``append_to_ledger``. Safe for concurrent use; a second writer process
    is excluded by the ledger's ``flock`` (``LedgerLockedError``).

    The index is built incrementally: each operation reads only the bytes
    appended since the last one, so other writers' lines appear on the next
    call. A file that shrank is re-read from the start.
    """

    def __init__(
        self,
        path: str | os.PathLike = "ledger.jsonl",
        namespace: str = DEFAULT_NAMESPACE,
        trusted: list | None = None,
        *,
        wait: bool = False,
        timeout: float | None = None,
    ) -> None:
        if trusted is None:
            if not valid_name(namespace):
                raise InvalidError("store configuration")
            self._trusted = None
        else:
            self._trusted = check_store_config(namespace, trusted)
        self._path = Path(path)
        if _ledger.is_ledger_store(self._path):
            raise InvalidError(
                f"{self._path} is a LedgerStore directory; use cll.log.ledger.LedgerEntryStore for it"
            )
        self._namespace = namespace
        self._wait = wait
        self._timeout = timeout
        self._lock = threading.RLock()
        self._closed = False
        self._reset()

    # -- index --------------------------------------------------------------

    def _reset(self) -> None:
        self._lines: list[_Line] = []
        self._first: dict[bytes, int] = {}
        self._consumed = 0
        self._tail: _Line | None = None

    def _require_open(self) -> None:
        if self._closed:
            raise ClosedError("backend is closed")

    def _parse(self, raw: bytes, offset: int, seq: int, previous: datetime) -> _Line | None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            logging.getLogger(__name__).warning(
                "legacy ledger: skipping corrupt line at byte %d in %s: %r", offset, self._path, text[:80]
            )
            return None
        if not isinstance(obj, dict):
            raise LogCorruptError(f"{self._path}: line at byte {offset} is not a JSON object")
        capsule_id = obj.get("capsule_id")
        try:
            value = bytes.fromhex(capsule_id) if isinstance(capsule_id, str) and len(capsule_id) == 64 else b""
        except ValueError:
            value = b""
        if len(value) != 32:
            raise LogCorruptError(f"{self._path}: line seq={seq} capsule_id is not a 32-byte hex digest")
        appended_at = _line_time(seq, obj) or previous
        return _Line(Entry(seq, value, appended_at), offset, len(raw), _is_capsule(obj))

    def _refresh(self) -> None:
        """Index lines appended since the last call. Caller holds ``_lock``."""
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < self._consumed:
            self._reset()
        if size == self._consumed and self._tail is None:
            return
        data = b""
        if size > self._consumed:
            with open(self._path, "rb") as fh:
                fh.seek(self._consumed)
                data = fh.read(size - self._consumed)
        complete = data.rfind(b"\n") + 1
        offset = self._consumed
        for raw in data[:complete].split(b"\n")[:-1]:
            self._index(self._parse(raw, offset, len(self._lines) + 1, self._last_time()))
            offset += len(raw) + 1
        self._consumed = offset
        # A torn last line (no newline yet) still counts when it parses, as
        # it always has for read_ledger_entries, but is re-read next time.
        tail = data[complete:]
        self._tail = self._parse(tail, offset, len(self._lines) + 1, self._last_time()) if tail else None

    def _index(self, line: _Line | None) -> None:
        if line is None:
            return
        self._lines.append(line)
        self._first.setdefault(line.entry.value, len(self._lines) - 1)

    def _last_time(self) -> datetime:
        return self._lines[-1].entry.appended_at if self._lines else _EPOCH

    def _all(self) -> list:
        return self._lines + [self._tail] if self._tail is not None else self._lines

    def _find(self, value: bytes) -> _Line | None:
        index = self._first.get(value)
        if index is not None:
            return self._lines[index]
        if self._tail is not None and self._tail.entry.value == value:
            return self._tail
        return None

    def _read_line(self, line: _Line) -> dict:
        with open(self._path, "rb") as fh:
            fh.seek(line.offset)
            raw = fh.read(line.length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LogCorruptError(f"{self._path}: line seq={line.entry.seq} changed on disk") from exc
        if not isinstance(obj, dict):
            raise LogCorruptError(f"{self._path}: line seq={line.entry.seq} changed on disk")
        return obj

    def entries(self) -> Iterator[Entry]:
        """Every entry in sequence order (the checkpoint index's source)."""
        with self._lock:
            self._require_open()
            self._refresh()
            lines = list(self._all())
        return (line.entry for line in lines)

    # -- CLL entry log (cll.log.EntryStore) ------------------------------------

    def scan_entries(self, after_seq: int, limit: int) -> list:
        with self._lock:
            self._require_open()
            check_scan(after_seq, limit)
            self._refresh()
            lines = self._lines[after_seq : after_seq + limit]
            if self._tail is not None and len(lines) < limit and self._tail.entry.seq > after_seq:
                lines.append(self._tail)
            return [line.entry for line in lines]

    def get_entry(self, value: bytes) -> Entry:
        with self._lock:
            self._require_open()
            value = check_value(value)
            self._refresh()
            line = self._find(value)
            if line is None:
                raise LogNotFoundError("entry not found")
            return line.entry

    def append(self, input: AppendInput) -> AppendResult:
        with self._lock:
            self._require_open()
            value, _ = check_append(input)
            self._refresh()
            line = self._find(value)
            if line is None:
                raise LogUnsupportedError(
                    "the legacy ledger cannot append a bare digest; write the capsule with put()"
                )
            return AppendResult(line.entry, IDEMPOTENT)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # -- artifact store (capsule_emit.artifact.Store) --------------------------

    @property
    def namespace(self) -> str:
        return self._namespace

    def init(self) -> None:
        """Creates an empty ledger file when none exists."""
        self._require_open()
        self._path.touch(exist_ok=True)

    def put(self, record: Record) -> None:
        self.put_entry(record)

    def put_entry(self, record: Record) -> AppendResult:
        """``put``, returning the capsule's log entry and whether this call
        wrote it (``inserted``) or found it already stored (``idempotent``)."""
        self._require_open()
        prepared = prepare(record)
        if prepared.artifacts:
            raise UnsupportedError("the legacy ledger keeps no originals")
        if not valid_id(prepared.capsule_id):
            raise InvalidError("capsule id")
        try:
            capsule = decode_strict_json(prepared.capsule)
        except _StrictJSONError as exc:
            raise InvalidError(f"capsule verification: {exc}") from exc
        if not isinstance(capsule, dict):
            raise InvalidError("capsule verification: capsule is not an object")
        if any(name in capsule for name in _ENVELOPE_MEMBERS):
            raise InvalidError("capsule verification: signature and key_id are not capsule members")
        if _line_form(capsule).encode("ascii") != prepared.capsule:
            raise InvalidError("capsule bytes are not in the ledger's line form")
        key_id = _verify_capsule(prepared.capsule_id, capsule, prepared.producer_envelope, self._trusted)
        line = dict(capsule)
        if key_id is not None:
            line["signature"] = bytes(prepared.producer_envelope).hex()
            line["key_id"] = key_id
        return self.append_line(line)

    def append_line(self, line: dict) -> AppendResult:
        """Write one capsule line (with any inline ``signature``/``key_id``)
        unless its ``capsule_id`` is already in the ledger. ``put`` calls it
        after verification; ``seal()`` and ``log()`` call it directly, because
        a custom ``Signer`` may produce a signature that is not a COSE
        envelope, which the ledger has always accepted (``get`` then reports
        that line ``invalid``). An existing identical line is ``idempotent``;
        any other line under the same ``capsule_id`` is ``ConflictError``."""
        self._require_open()
        capsule_id = line.get("capsule_id")
        if not valid_id(capsule_id) or not _is_capsule(line):
            raise InvalidError("capsule id")
        text = _line_form(line)
        value = bytes.fromhex(capsule_id)
        with _ledger._append_lock, _ledger._writer_lock(self._path, wait=self._wait, timeout=self._timeout):
            with self._lock:
                self._require_open()
                self._refresh()
                existing = self._find(value)
                if existing is not None:
                    if not existing.capsule or _line_form(self._read_line(existing)) != text:
                        raise ConflictError("immutable record conflict")
                    return AppendResult(existing.entry, IDEMPOTENT)
                _ledger._write_line(self._path, text)
                self._refresh()
                written = self._find(value)
                if written is None:  # pragma: no cover - the line was just written
                    raise LogCorruptError(f"{self._path}: written line is missing")
                return AppendResult(written.entry, INSERTED)

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock:
            self._require_open()
            self._refresh()
            line = self._find(bytes.fromhex(capsule_id))
            if line is None or not line.capsule:
                raise NotFoundError("capsule not found")
            return self._record(line)

    def _record(self, line: _Line) -> Record:
        """The verified record a line holds. Caller holds ``_lock``."""
        obj = self._read_line(line)
        signature = obj.pop("signature", None)
        key_id = obj.pop("key_id", None)
        capsule_id = line.entry.value.hex()
        if (signature is None) != (key_id is None):
            raise InvalidError("envelope: only one of signature and key_id is present")
        envelope = b""
        if signature is not None:
            try:
                envelope = bytes.fromhex(signature)
            except (TypeError, ValueError) as exc:
                raise InvalidError("envelope: signature is not hex") from exc
        signer = _verify_capsule(capsule_id, obj, envelope, self._trusted)
        if signer is not None and signer != str(key_id).lower():
            raise InvalidError("envelope: key_id is not the envelope's signer")
        return Record(capsule_id, _line_form(obj).encode("ascii"), envelope, [])

    def purge(self, capsule_id: str) -> None:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        self._require_open()
        raise UnsupportedError("the legacy ledger keeps no originals to purge")


_shared_lock = threading.Lock()
_shared: dict[str, LegacyLedger] = {}


def shared(path: str | os.PathLike) -> LegacyLedger:
    """The process-wide ``LegacyLedger`` for ``path``, so ``seal()`` and the
    checkpoint index reuse one incremental index per ledger file."""
    key = os.path.abspath(os.fspath(path))
    with _shared_lock:
        store = _shared.get(key)
        if store is None:
            store = _shared[key] = LegacyLedger(path)
        return store
