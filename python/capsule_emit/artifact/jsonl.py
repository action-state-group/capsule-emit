# SPDX-License-Identifier: Apache-2.0
"""JSONL artifact store: one file per namespace, one record per line.

The file format is byte-compatible with the Go ``artifact/jsonl`` and
TypeScript ``./artifact/jsonl`` stores in either direction (see
``docs/artifact-store.md``). The namespace is a construction-time scope and is
not stored per line. Put appends one line; purge rewrites the file through a
temporary file and an atomic rename. Single writer: no multi-process locking.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterable

from .record import (
    ConflictError,
    CorruptError,
    InvalidError,
    NotFoundError,
    PurgedError,
    Record,
    check_store_config,
    decode_record,
    encode_record,
    has_purged,
    prepare,
    purged_copy,
    storage_checksum,
    valid_id,
    verify,
)

__all__ = ["JsonlArtifactStore", "decode_record", "encode_record"]

_Span = tuple[int, int]  # (offset, length) of a line, excluding its "\n"


def _peek_id(line: bytes) -> str | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("capsule_id"), str) and value["capsule_id"]:
        return value["capsule_id"]
    return None


class JsonlArtifactStore:
    """A file-backed artifact store. Opening reads an existing file once to
    index ``capsule_id`` to line offsets (last occurrence wins); it does not
    create the file, which :meth:`init` does."""

    def __init__(self, path: os.PathLike[str] | str, namespace: str, trusted: Iterable[bytes]) -> None:
        self._trusted = check_store_config(namespace, trusted)
        self._namespace = namespace
        self._path = os.fspath(path)
        self._lock = threading.Lock()
        self._index: dict = {}
        if os.path.exists(self._path):
            with open(self._path, "rb") as f:
                self._index = self._scan(f.read())[0]

    @property
    def namespace(self) -> str:
        return self._namespace

    @staticmethod
    def _scan(data: bytes) -> tuple[dict, list]:
        """Indexes complete lines. Returns the index and each line's span."""
        index: dict = {}
        spans: list = []
        offset = 0
        while True:
            end = data.find(b"\n", offset)
            if end < 0:
                break  # a trailing partial line is left unindexed
            line = data[offset:end]
            capsule_id = _peek_id(line) if line else None
            if capsule_id is not None:
                index[capsule_id] = (offset, end - offset)
            spans.append((offset, end - offset, capsule_id))
            offset = end + 1
        return index, spans

    def init(self) -> None:
        """Creates an empty file if none exists; an existing file is untouched."""
        with self._lock:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return
            os.close(fd)

    def _read_at(self, span: _Span) -> Record:
        offset, length = span
        with open(self._path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        if len(data) != length:
            raise CorruptError("read failed: short read")
        try:
            return decode_record(data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CorruptError("parse failed") from exc

    def _read_indexed(self, capsule_id: str) -> Record:
        record = self._read_at(self._index[capsule_id])
        if record.capsule_id != capsule_id:
            raise CorruptError("index points to a different capsule")
        return record

    def put(self, record: Record) -> None:
        prepared = prepare(record)
        verify(prepared, self._trusted)
        checksum = storage_checksum(prepared)
        with self._lock:
            if prepared.capsule_id in self._index:
                existing = self._read_indexed(prepared.capsule_id)
                verify(existing, self._trusted)  # reads fail closed
                if storage_checksum(existing) != checksum:
                    raise ConflictError("immutable record conflict")
                if has_purged(existing):
                    raise PurgedError("originals were purged")
                return
            line = encode_record(prepared).encode("utf-8")
            # O_APPEND without O_CREAT: an uninitialized store fails instead of
            # silently creating its file.
            fd = os.open(self._path, os.O_RDWR | os.O_APPEND)
            try:
                size = os.fstat(fd).st_size
                # Terminate a torn last line first, so the new record starts
                # its own line instead of merging into the damaged one.
                prefix = b""
                if size:
                    os.lseek(fd, size - 1, os.SEEK_SET)
                    if os.read(fd, 1) != b"\n":
                        prefix = b"\n"
                pending = memoryview(prefix + line + b"\n")
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise OSError("short write appending to the JSONL store")
                    pending = pending[written:]
            finally:
                os.close(fd)
            self._index[prepared.capsule_id] = (size + len(prefix), len(line))

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock:
            if capsule_id not in self._index:
                raise NotFoundError("capsule not found")
            record = self._read_indexed(capsule_id)
        verify(record, self._trusted)
        return record

    def purge(self, capsule_id: str) -> None:
        """Rewrites the record's line with present originals tombstoned.
        Requires only the record's existence, not its verification."""
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        with self._lock:
            if capsule_id not in self._index:
                raise NotFoundError("capsule not found")
            replacement = encode_record(purged_copy(self._read_indexed(capsule_id))).encode("utf-8")
            self._rewrite(capsule_id, replacement)

    def _rewrite(self, capsule_id: str, replacement: bytes) -> None:
        with open(self._path, "rb") as f:
            data = f.read()
        mode = stat.S_IMODE(os.stat(self._path).st_mode)
        _, spans = self._scan(data)
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(prefix=".jsonl-rewrite-", dir=directory)
        committed = False
        try:
            index: dict = {}
            position = 0
            found = False
            with os.fdopen(fd, "wb") as out:
                os.chmod(tmp, mode)
                for offset, length, line_id in spans:
                    line = data[offset : offset + length]
                    if line_id == capsule_id:
                        found, line = True, replacement
                    out.write(line + b"\n")
                    if line_id is not None:
                        index[line_id] = (position, len(line))
                    position += len(line) + 1
                # Keep a torn trailing line (no "\n") byte for byte; it stays
                # unindexed, as it was before the rewrite.
                out.write(data[spans[-1][0] + spans[-1][1] + 1 :] if spans else data)
                out.flush()
                os.fsync(out.fileno())
            if not found:
                raise CorruptError("target line vanished during rewrite")
            os.replace(tmp, self._path)
            committed = True
            self._index = index
        finally:
            if not committed:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
