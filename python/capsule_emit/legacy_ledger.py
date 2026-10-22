# SPDX-License-Identifier: Apache-2.0
"""The flat ``ledger.jsonl`` as a backend for both storage interfaces.

The same backend also serves a ledger kept in a ledger store directory
(``capsule_emit.ledger_store``): its lines are read from the store's segments
and appended through the store, and everything below holds for it, except
that a store's entries are its complete lines, the first per ``capsule_id``.

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

Checkpoint state
----------------

``load_cll`` reads the CLL checkpoint state (``cll.log.CllState``) the ledger
already holds, without writing: the newest checkpoint-stamp line whose sibling
``checkpoint_cose`` (the hex COSE_Sign1 statement ``capsule_emit.witness``
persists beside the JSON checkpoint) verifies offline and covers no more
leaves than the ledger has. Its MMR nodes are folded from the ledger's own
entries up to the checkpoint's size and must rebuild the signed root, or the
ledger is ``corrupt``. A ledger with no such stamp has no checkpoint
(``checkpoint`` is ``None``). With ``cll.log.CheckpointStateStore``'s reader
and the artifact ``get`` above, ``agent_action_capsule.bundle.
proof_source_from_cll(ledger, ledger)`` builds Evidence Bundles over the
file. ``commit_cll`` is ``unsupported``: checkpoint stamps keep their own
writer.

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
producer envelopes) come back with an empty ``producer_envelope``. A padding
line (``record_type: "padding"``) is a record too, checked as the artifact
store contract checks padding records and always with its envelope; ``put``
appends one and ``get`` returns it. Checkpoint stamps, disclosure records and
witness backfills are log entries only: ``get`` reports them ``not_found``.

The capsule is checked with capsule-emit's own rules, which keep both
canonicalization profiles: ``capsule_id`` is recomputed per
``canonicalization_id`` (``jcs``, or ``jcs-n`` when it is absent); a ``jcs``
capsule must also pass Class 1 verification; and a claimed envelope (either
member present) must be complete and verify over that ``capsule_id`` with
``key_id`` as its signer. With ``trusted`` keys the signer must also be one of
them (``untrusted_signer``); without them, any valid signer is accepted, as
``seal()`` itself accepts any configured key. Lines are read as strict JSON.

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

import contextlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cll.log import (
    IDEMPOTENT,
    INSERTED,
    AppendInput,
    AppendResult,
    CheckpointState,
    CllState,
    ClosedError,
    Entry,
)
from cll.log import CorruptError as LogCorruptError
from cll.log import NotFoundError as LogNotFoundError
from cll.log import UnsupportedError as LogUnsupportedError
from cll.log.engine import check_append, check_scan, check_value
from cll.log.ledger import capsule_timestamp

from . import ledger as _ledger
from . import ledger_store as _store
from .artifact.record import (
    PADDING_RECORD_TYPE,
    ConflictError,
    CorruptError,
    InvalidError,
    NotFoundError,
    Record,
    UnsupportedError,
    UntrustedSignerError,
    _StrictJSONError,
    _verify_padding,
    check_store_config,
    decode_strict_json,
    prepare,
    valid_id,
    valid_name,
)
from .canonicalization import (
    CANONICALIZATION_JCS,
    UnsupportedCanonicalizationError,
    compute_capsule_id,
    resolve_canonicalization_id,
)
from .verification import verify_capsule

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
    #: The store segment holding the line; None in a flat file.
    segment: str | None = None


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


@contextlib.contextmanager
def _as_artifact_errors():
    """Reports a corrupt ledger line as the artifact contract's ``corrupt``
    on artifact operations (the CLL operations keep ``cll.log`` codes)."""
    try:
        yield
    except LogCorruptError as exc:
        raise CorruptError(str(exc)) from exc


def _cll_state_at(values: list, cose: bytes) -> CllState:
    """The CLL state of the checkpoint ``cose`` (a COSE_Sign1 checkpoint
    statement, already verified by the caller) over a log whose leaf values,
    in order, are ``values``: MMR nodes folded from the first
    ``leaf_count(mmr_size)`` values, checked against the signed root. Raises
    ``cll.log.CorruptError`` when the values do not rebuild that root, and
    ``ValueError`` when the statement does not decode or covers more leaves
    than ``values`` holds."""
    from cll.checkpoint import (
        MemoryNodeStore,
        add_leaf,
        leaf_count,
        leaf_hash,
        peaks,
        root_from_peaks,
        verify_checkpoint_cose_offline,
    )

    checked = verify_checkpoint_cose_offline(cose)
    if not checked.ok or checked.decoded is None:
        raise ValueError("the checkpoint statement does not verify")
    signed = checked.decoded
    tip = leaf_count(signed.mmr_size)
    if tip > len(values):
        raise ValueError(f"the checkpoint covers {tip} leaves; the log has {len(values)}")
    nodes = MemoryNodeStore()
    for value in values[:tip]:
        add_leaf(nodes, leaf_hash(bytes(value)))
    folded = [nodes.node(pos) for pos in peaks(signed.mmr_size)]
    if nodes.size() != signed.mmr_size or root_from_peaks(folded).hex() != signed.root:
        raise LogCorruptError(
            f"the log's first {tip} leaves do not rebuild the root of its signed checkpoint "
            f"(mmr_size {signed.mmr_size})"
        )
    node_hashes = tuple(nodes.node(pos) for pos in range(nodes.size()))
    return CllState(
        size=nodes.size(),
        nodes=node_hashes,
        indexed_seq=tip,
        checkpoint=CheckpointState(bytes=bytes(cose), size=signed.mmr_size, indexed_seq=tip, peaks=tuple(folded)),
    )


def _is_record(obj: dict) -> bool:
    """Whether a line is a record ``get`` returns: a capsule or a padding
    record, not a checkpoint stamp, disclosure record or witness backfill."""
    return obj.get("kind") not in _ledger.NON_CAPSULE_KINDS


def _line_form(value: dict) -> str:
    """The exact text ``append_to_ledger`` writes for ``value``."""
    return json.dumps(value, separators=(",", ":"))


def _verify_capsule(capsule_id: str, capsule: dict, envelope: bytes | None, trusted: list | None) -> str | None:
    """capsule-emit's legacy check: the content address per profile, Class 1
    for the ``jcs`` profile, and the envelope when one is claimed (``None``
    means none is). Returns the signer ``key_id`` (hex), or ``None`` for an
    unsigned capsule."""
    if capsule.get("capsule_id") != capsule_id:
        raise InvalidError("capsule verification: carried capsule_id differs")
    if capsule.get("record_type") == PADDING_RECORD_TYPE:
        # A padding record, as the artifact store contract checks it; it is
        # never written without its envelope.
        _verify_padding(capsule_id, capsule)
        if envelope is None:
            raise InvalidError("envelope: a padding record carries its producer envelope")
    else:
        try:
            computed = compute_capsule_id(capsule)
        except (UnsupportedCanonicalizationError, ValueError, TypeError) as exc:
            raise InvalidError(f"capsule verification: {exc}") from exc
        if computed != capsule_id:
            raise InvalidError("capsule verification: capsule id mismatch")
        if resolve_canonicalization_id(capsule) == CANONICALIZATION_JCS:
            result = verify_capsule(capsule)
            if not result.ok:
                raise InvalidError("capsule verification: " + ",".join(f.code for f in result.errors))
    if envelope is None:
        return None
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    result = verify_producer_envelope(capsule_id, bytes(envelope))
    if not result.ok or result.public_key is None:
        raise InvalidError("envelope: " + ",".join(f.code for f in result.findings))
    if trusted is not None and result.public_key not in trusted:
        raise UntrustedSignerError("producer signer is not trusted")
    return result.public_key.hex()


def checked_record(capsule_id: str, obj: dict, trusted: list | None = None) -> Record:
    """The verified record a capsule line holds: the capsule without its
    inline ``signature`` and ``key_id``, and the Producer Envelope they claim.
    A claim must be complete (both members, non-empty hex), verify over the
    capsule ID and name its own signer as ``key_id``; a line with neither is
    unsigned. Raises the artifact ``InvalidError`` (or ``UntrustedSignerError``)
    otherwise."""
    obj = dict(obj)
    claimed = [name in obj for name in _ENVELOPE_MEMBERS]
    signature = obj.pop("signature", None)
    key_id = obj.pop("key_id", None)
    envelope = None
    if any(claimed):
        if not all(claimed) or not isinstance(signature, str) or not isinstance(key_id, str):
            raise InvalidError("envelope: signature and key_id must both be present strings")
        try:
            envelope = bytes.fromhex(signature)
        except ValueError as exc:
            raise InvalidError("envelope: signature is not hex") from exc
        if not envelope:
            raise InvalidError("envelope: signature is empty")
    signer = _verify_capsule(capsule_id, obj, envelope, trusted)
    if signer is not None and signer != key_id.lower():
        raise InvalidError("envelope: key_id is not the envelope's signer")
    return Record(capsule_id, _line_form(obj).encode("ascii"), envelope or b"", [])


class LegacyLedger:
    """A ``ledger.jsonl`` file as a CLL ``EntryStore`` and an artifact
    ``Store``, with the capability limits in ``CAPABILITIES``. ``path``
    defaults to the ledger ``seal()`` writes, resolved when it is constructed
    (``capsule_emit.ledger_layout.resolve_ledger``).

    ``trusted`` (raw 32-byte Ed25519 public keys) turns on signer
    authorization; ``None`` accepts any signer whose envelope verifies.
    ``wait``/``timeout`` are passed to the writer lock, as for
    ``append_to_ledger``. Safe for concurrent use; a second writer process
    is excluded by the ledger's ``flock`` (``LedgerLockedError``).

    The index is built incrementally: each operation reads only the bytes
    appended since the last one, so other writers' lines appear on the next
    call. A file that shrank or was replaced (another inode) is re-read from
    the start. On artifact operations a corrupt line is the artifact
    ``CorruptError``; on CLL operations it is ``cll.log.CorruptError``.
    """

    def __init__(
        self,
        path: str | os.PathLike | None = None,
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
        # A directory is a ledger store (a managed LedgerStore, or an empty
        # directory that becomes one), and so is a path that does not exist
        # and whose name has no '.'; anything else is a flat file. A store
        # is addressed by its canonical path, so every spelling shares one
        # lock; a flat file keeps the spelling it was given.
        self._store: _store.StoreLines | None = None
        if path is None:  # the default ledger, as seal() resolves it now
            from .ledger_layout import resolve_ledger

            path = resolve_ledger(None).path
        if Path(path).is_dir() or _store.is_store_target(path):
            if Path(path).is_dir() and not _store.is_store_path(path):
                raise _store.LedgerLayoutError(
                    f"{path} is a directory that is neither a ledger store nor empty"
                )
            self._path = _store.store_root(path)
            self._store = _store.StoreLines(self._path)
        else:
            self._path = Path(path)
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
        self._identity: tuple | None = None

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
        return _Line(Entry(seq, value, appended_at), offset, len(raw), _is_record(obj))

    def _refresh(self) -> None:
        """Index lines appended since the last call. Caller holds ``_lock``."""
        if self._store is not None:
            self._refresh_store()
            return
        try:
            stat = self._path.stat()
            size, identity = stat.st_size, (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            size, identity = 0, None
        # A shrunk or replaced file (another inode) is read again from the
        # start. Rewriting earlier bytes of the same file in place is not
        # detected: the ledger is append-only.
        if size < self._consumed or (self._identity is not None and identity != self._identity):
            self._reset()
        self._identity = identity
        if size == self._consumed and self._tail is None:
            return
        data = b""
        if size > self._consumed:
            with open(self._path, "rb") as fh:
                fh.seek(self._consumed)
                data = fh.read(size - self._consumed)
        complete = data.rfind(b"\n") + 1
        offset = self._consumed
        seq = len(self._lines) + 1
        previous = self._last_time()
        parsed = []
        # Parse the whole chunk before indexing any of it, so a corrupt line
        # leaves the index exactly as it was.
        for raw in data[:complete].split(b"\n")[:-1]:
            line = self._parse(raw, offset, seq, previous)
            offset += len(raw) + 1
            if line is not None:
                parsed.append(line)
                seq += 1
                previous = line.entry.appended_at
        # A torn last line (no newline yet) still counts when it parses, as
        # it always has for read_ledger_entries, but is re-read next time.
        tail = data[complete:]
        tail_line = self._parse(tail, offset, seq, previous) if tail else None
        for line in parsed:
            self._lines.append(line)
            self._first.setdefault(line.entry.value, len(self._lines) - 1)
        self._consumed = offset
        self._tail = tail_line

    def _refresh_store(self) -> None:
        """``_refresh`` for a ledger store: its entries are complete lines,
        the first per ``capsule_id`` (``ledger_store.StoreLines``). An empty
        directory has none yet."""
        if not (self._path / _store.MANIFEST_FILENAME).is_file():
            # No store (yet, or any more): forget what was read, so a store
            # created here later is read from its start.
            self._store = _store.StoreLines(self._path)
            self._reset()
            return
        try:
            restarted, added = self._store.refresh()
            if restarted:
                self._reset()
            seq = len(self._lines) + 1
            previous = self._last_time()
            parsed = []
            for raw_line in added:
                line = self._parse(raw_line.raw, raw_line.offset, seq, previous)
                if line is None:
                    continue
                parsed.append(_Line(line.entry, line.offset, line.length, line.capsule, raw_line.segment))
                seq += 1
                previous = line.entry.appended_at
        except BaseException:
            # The source already counted these lines as read: forget them
            # both there and here, so the next call reads them again (and
            # fails the same way) rather than skipping them.
            self._store = _store.StoreLines(self._path)
            self._reset()
            raise
        for line in parsed:
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

    def _read_raw(self, line: _Line) -> bytes:
        if line.segment is not None:
            with open(self._path / "segments" / line.segment, "rb") as fh:
                fh.seek(line.offset)
                return fh.read(line.length).strip()
        with open(self._path, "rb") as fh:
            fh.seek(line.offset)
            return fh.read(line.length).strip()

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

    # -- CLL checkpoint state (cll.log.CheckpointStateStore), read-only -------

    def load_cll(self) -> CllState:
        """The newest checkpoint-stamp line whose ``checkpoint_cose``
        verifies and covers no more leaves than the ledger holds, as CLL
        state, with MMR nodes folded from the ledger's own entries (see the
        module docstring). No such stamp: a state with ``checkpoint=None``.
        Raises ``cll.log.CorruptError`` when the entries contradict the
        signed root."""
        with self._lock:
            self._require_open()
            self._refresh()
            lines = list(self._all())
            values = [line.entry.value for line in lines]
            for line in reversed(lines):
                if line.capsule:
                    continue
                cose = self._stamp_cose(line)
                if cose is None:
                    continue
                try:
                    return _cll_state_at(values, cose)
                except ValueError:
                    continue
            return CllState()

    def _stamp_cose(self, line: _Line) -> bytes | None:
        """The COSE statement a checkpoint-stamp line carries, or ``None``
        for any other line or a stamp without one. Caller holds ``_lock``."""
        try:
            obj = json.loads(self._read_raw(line))
        except ValueError:
            return None
        if not isinstance(obj, dict) or obj.get("kind") != _ledger.CHECKPOINT_STAMP_KIND:
            return None
        cose_hex = obj.get("checkpoint_cose")
        if not isinstance(cose_hex, str) or not cose_hex:
            return None
        try:
            return bytes.fromhex(cose_hex)
        except ValueError:
            return None

    def commit_cll(self, expected_size: int, expected_checkpoint: bytes | None, next: CllState) -> None:
        self._require_open()
        raise LogUnsupportedError(
            "the legacy ledger's checkpoint state is its checkpoint-stamp lines; "
            "capsule_emit.witness writes them"
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # -- artifact store (capsule_emit.artifact.Store) --------------------------

    @property
    def namespace(self) -> str:
        return self._namespace

    def init(self) -> None:
        """Creates an empty ledger when none exists: an empty file, or for a
        store path an empty capsule-emit store (never a regular file)."""
        self._require_open()
        if self._store is not None:
            if not (self._path / _store.MANIFEST_FILENAME).is_file():
                with _ledger._append_lock, _ledger._writer_lock(self._path, wait=self._wait, timeout=self._timeout):
                    if not (self._path / _store.MANIFEST_FILENAME).is_file():
                        _store.create_store(self._path)
            return
        _refuse_new_flat_file(self._path)
        self._path.touch(exist_ok=True)

    def put(self, record: Record) -> None:
        from .ledger_layout import FlatLedgerWriteRefused

        try:
            self.put_entry(record)
        except FlatLedgerWriteRefused as exc:  # the flat ledger's window has closed
            raise UnsupportedError(str(exc)) from exc

    def put_entry(self, record: Record) -> AppendResult:
        """``put``, returning the capsule's log entry and whether this call
        wrote it (``inserted``) or found it already stored (``idempotent``)."""
        self._require_open()
        prepared = prepare(record)
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
        envelope = bytes(prepared.producer_envelope) or None
        key_id = _verify_capsule(prepared.capsule_id, capsule, envelope, self._trusted)
        if prepared.artifacts:
            raise UnsupportedError("the legacy ledger keeps no originals")
        line = dict(capsule)
        if key_id is not None:
            line["signature"] = bytes(prepared.producer_envelope).hex()
            line["key_id"] = key_id
        return self.append_line(line)

    def append_any(
        self, line: dict, *, wait: bool | None = None, timeout: float | None = None, precheck: Any = None
    ) -> AppendResult:
        """Write any ledger line, bookkeeping included (checkpoint stamps,
        witness backfills, disclosure records): ``append_line`` without its
        record check. ``capsule_emit.ledger.append_to_ledger`` calls it, so an
        identical line is ``idempotent`` and a different line under the same
        ``capsule_id`` is refused, for every kind of line."""
        return self._write(line, require_record=False, wait=wait, timeout=timeout, precheck=precheck)

    def append_line(self, line: dict, *, precheck: Any = None) -> AppendResult:
        """Write one capsule line (with any inline ``signature``/``key_id``)
        unless its ``capsule_id`` is already in the ledger. ``put`` calls it
        after verification; ``seal()`` and ``log()`` call it directly, because
        a custom ``Signer`` may produce a signature that is not a COSE
        envelope, which the ledger has always accepted (``get`` then reports
        that line ``invalid``). An existing identical line is ``idempotent``;
        any other line under the same ``capsule_id`` is ``ConflictError``."""
        return self._write(line, require_record=True, precheck=precheck)

    def _write(
        self,
        line: dict,
        *,
        require_record: bool,
        wait: bool | None = None,
        timeout: float | None = None,
        precheck: Any = None,
    ) -> AppendResult:
        self._require_open()
        capsule_id = line.get("capsule_id")
        if not valid_id(capsule_id) or (require_record and not _is_record(line)):
            raise InvalidError("capsule id")
        text = _line_form(line)
        value = bytes.fromhex(capsule_id)
        if self._store is None and self._path.exists() and line.get("kind") not in _ledger.NON_CAPSULE_KINDS:
            # A record to a flat ledger, by any entry point: the deprecation
            # window applies (bookkeeping never stops). Checked before the
            # locks are taken, so a warnings hook that writes a ledger cannot
            # deadlock on them.
            from .ledger_layout import check_new_flat_record

            check_new_flat_record(self._path)
        lock = _ledger._writer_lock(
            self._path,
            wait=self._wait if wait is None else wait,
            timeout=self._timeout if wait is None else timeout,
        )
        with _ledger._append_lock, lock, self._lock, _as_artifact_errors():
            self._require_open()
            if precheck is not None:
                precheck()  # may raise, writing nothing (ledger_layout.DefaultMoved)
            if self._store is not None:
                # A crashed append may have left this very line written but
                # unindexed: recover first, so "already present" also means
                # indexed.
                _store.reconcile(self._path)
            self._refresh()
            existing = self._find(value)
            if existing is not None:
                if (require_record and not existing.capsule) or self._read_raw(existing) != text.encode("ascii"):
                    raise ConflictError("immutable record conflict")
                return AppendResult(existing.entry, IDEMPOTENT)
            if self._store is not None:
                _store.append_line(self._path, text)
            else:
                _refuse_new_flat_file(self._path)
                _ledger._write_line(self._path, text)
            self._refresh()
            written = self._find(value)
            if written is None:  # pragma: no cover - the line was just written
                raise LogCorruptError(f"{self._path}: written line is missing")
            return AppendResult(written.entry, INSERTED)

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock, _as_artifact_errors():
            self._require_open()
            self._refresh()
            line = self._find(bytes.fromhex(capsule_id))
            if line is None or not line.capsule:
                raise NotFoundError("capsule not found")
            return self._record(line)

    def _record(self, line: _Line) -> Record:
        """The verified record a line holds. Caller holds ``_lock``."""
        try:
            obj = decode_strict_json(self._read_raw(line))
        except _StrictJSONError as exc:
            raise InvalidError(f"capsule verification: {exc}") from exc
        if not isinstance(obj, dict):
            raise InvalidError("capsule verification: capsule is not an object")
        return checked_record(line.entry.value.hex(), obj, self._trusted)

    def purge(self, capsule_id: str) -> None:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        self._require_open()
        raise UnsupportedError("the legacy ledger keeps no originals to purge")


def _refuse_new_flat_file(path: Path) -> None:
    """No new flat ledger is created (capsule-emit#358): writing to a flat
    path that does not exist yet raises ``FlatLedgerWriteRefused``."""
    from . import ledger_layout as _layout

    if not path.exists() and not _layout._allow_new_flat:
        from .ledger_migrate import suggested_store

        raise _layout.FlatLedgerWriteRefused(
            f"{path} does not exist, and capsule-emit no longer creates flat ledgers. "
            f'Use a ledger directory instead, for example "{suggested_store(path)}".'
        )


_shared_lock = threading.Lock()
_shared: dict[str, LegacyLedger] = {}


def shared(path: str | os.PathLike) -> LegacyLedger:
    """The process-wide ``LegacyLedger`` for ``path``, so ``seal()`` and the
    checkpoint index reuse one incremental index per ledger file."""
    key = os.fspath(_store.store_root(path)) if _store.is_store_target(path) else os.path.abspath(os.fspath(path))
    with _shared_lock:
        store = _shared.get(key)
        if store is None or store._closed:
            store = _shared[key] = LegacyLedger(key)
        return store
