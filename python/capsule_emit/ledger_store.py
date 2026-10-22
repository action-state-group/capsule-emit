# SPDX-License-Identifier: Apache-2.0
"""A ledger kept in checkpointed-local-log's managed ``LedgerStore``.

The store is a directory: ``manifest.json`` lists JSONL segments under
``segments/``, the source of truth, and CLL keeps SQLite indexes beside them.
capsule-emit writes the same lines it writes to a flat ``ledger.jsonl``
(capsules, ``log()`` entries, checkpoint stamps, witness backfills, disclosure
records, holds, padding), so the leaves, ``capsule_id`` values and checkpoint
roots are the same for the same writes. See ``docs/legacy-ledger.md``.

**Reading** never opens SQLite: :class:`StoreLines` reads ``manifest.json`` and
then each segment, so reads create no files and work on a read-only copy. A
store's entries are its complete lines, the first per ``capsule_id`` (the set
the store's UNIQUE index admits): the active segment's unterminated last line
is not an entry yet, and a later line under an id already seen (a duplicate
append left it before CLL checked first) is skipped. An archived (unmounted)
segment hides its ids, so positions after it cannot be established:
:class:`StoreLines` raises :class:`LedgerArchivedError`, and
:func:`read_entries` shows a placeholder and the readable lines after it.

**Writing** goes only to stores capsule-emit created, which carry
``capsule-emit-store.json``; any other managed store is read and never written
(:class:`LedgerLayoutError`). The caller holds the ledger's writer lock
(``capsule_emit.ledger._writer_lock``). Each append runs CLL's ``recover()`` (a
torn tail or an unindexed line left by a crash), then appends with an fsync,
opening the store for the call and closing it after, so a second process never
acts on a stale manifest. The
store never rotates (an unbounded segment size), so it needs no checkpointer.
A new store is built in a fresh staging directory beside its path and renamed
into place, so an interrupted first write never leaves a half-made store.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MARKER_FILENAME",
    "MANIFEST_FILENAME",
    "LedgerArchivedError",
    "LedgerConflictError",
    "LedgerLayoutError",
    "LedgerWriteError",
    "StoreLine",
    "StoreLines",
    "append_line",
    "create_store",
    "is_marked",
    "is_store_path",
    "read_entries",
    "reconcile",
    "store_root",
]

#: The file capsule-emit writes into each store it creates. Only stores that
#: carry it are written to.
MARKER_FILENAME = "capsule-emit-store.json"
MANIFEST_FILENAME = "manifest.json"
_MARKER = {"v": 1}


class LedgerLayoutError(OSError):
    """A ledger path is not a layout capsule-emit can use for this operation:
    an invalid manifest, a directory that is neither a store nor empty, or a
    managed store capsule-emit did not create (read-only). An ``OSError``, so
    best-effort writers that catch ``OSError`` treat it like any write
    failure."""


class LedgerArchivedError(OSError):
    """A store segment is archived (unmounted): its entries' positions, and so
    proofs and appends, cannot be established until it is mounted."""


class LedgerWriteError(OSError):
    """A ledger line could not be written: it is not a valid line, the ledger
    holds a line that is not one, or the store could not record it. The cause
    is chained. An ``OSError``, so best-effort writers that catch ``OSError``
    (checkpoint stamps, witness backfills) never raise into a caller."""


class LedgerConflictError(OSError):
    """A different line is already in the ledger under the same
    ``capsule_id``; ledger lines are immutable."""


def store_root(path: str | os.PathLike) -> Path:
    """The canonical path of a store: symlinks resolved, trailing separator
    dropped, so every spelling of one store shares one lock and one key."""
    return Path(os.path.realpath(os.fspath(path)))


def is_store_path(path: str | os.PathLike) -> bool:
    """Whether ``path`` is a directory holding a managed store
    (``manifest.json``) or an empty directory, which becomes one."""
    p = Path(path)
    if not p.is_dir():
        return False
    return (p / MANIFEST_FILENAME).is_file() or not any(p.iterdir())


def is_marked(path: str | os.PathLike) -> bool:
    return (Path(path) / MARKER_FILENAME).is_file()


@dataclass(frozen=True)
class _Segment:
    name: str
    mounted: bool
    #: The manifest entry as written (its closing manifest, root, size).
    entry: dict


def _read_manifest(root: Path) -> tuple[list[_Segment], str]:
    """The validated segment list and active segment name of a managed store:
    unique plain file names, the active segment last and mounted, every
    mounted segment's file present."""
    path = root / MANIFEST_FILENAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc["segments"]
        active = doc["active_segment"]
        segments = [_Segment(e["name"], bool(e.get("mounted", True)), dict(e)) for e in entries]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LedgerLayoutError(f"{path} is not a ledger store manifest: {exc}") from exc
    names = [s.name for s in segments]
    if (
        not segments
        or not all(isinstance(n, str) and n and os.sep not in n and n not in (".", "..") for n in names)
        or len(set(names)) != len(names)
        or active != names[-1]
        or not segments[-1].mounted
    ):
        raise LedgerLayoutError(f"{path}: segments must be unique, ending with the mounted active segment")
    for segment in segments:
        if segment.mounted and not (root / "segments" / segment.name).is_file():
            raise LedgerLayoutError(f"{root / 'segments' / segment.name}: listed in the manifest but missing")
    return segments, active


@dataclass(frozen=True)
class StoreLine:
    """One entry of a store: its segment, byte offset and line bytes (no
    newline)."""

    segment: str
    offset: int
    raw: bytes


class StoreLines:
    """The entries of a managed store, read incrementally from its segments.

    ``refresh()`` returns the entries added since the last call, in order,
    and whether the store changed under the reader, in which case it was read
    again from the start.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._reset()

    def _reset(self) -> None:
        self._entries: list[StoreLine] = []
        self._seen: set[str] = set()
        self._segments: list[str] = []
        self._consumed: dict[str, int] = {}
        self._identity: dict[str, tuple[int, int]] = {}
        # The last line read from each segment (its offset and bytes): a
        # recreated store can reuse a segment's name and inode. Only that
        # last line is compared, so a recreated segment holding the very same
        # line at the same offset still reads as the old one grown.
        self._tail: dict[str, tuple[int, bytes]] = {}
        self._skipped: list[StoreLine] = []

    @property
    def skipped(self) -> list[StoreLine]:
        """Later lines under an id already seen, which are not entries."""
        return list(self._skipped)

    def all(self) -> list[StoreLine]:
        return list(self._entries)

    def refresh(self) -> tuple[bool, list[StoreLine]]:
        """Read what was appended since the last call: ``(restarted, added)``.
        ``restarted`` means the store changed under the reader (its segment
        list no longer extends the one read before, or a segment shrank), so
        everything was read again and ``added`` is every entry."""
        segments, active = _read_manifest(self._root)
        for segment in segments:
            if not segment.mounted:
                raise LedgerArchivedError(
                    f"{self._root}: segment {segment.name} is archived; mount it (cll segments mount) "
                    "to read positions and proofs"
                )
        names = [s.name for s in segments]

        def changed(name: str) -> bool:
            path = self._root / "segments" / name
            st = path.stat()
            if st.st_size < self._consumed[name] or (st.st_dev, st.st_ino) != self._identity[name]:
                return True
            if name not in self._tail:
                return False
            at, raw = self._tail[name]
            with open(path, "rb") as fh:
                fh.seek(at)
                return fh.read(len(raw)) != raw

        # A store deleted and recreated under the reader reuses segment names:
        # a segment that is another file (or shorter) restarts the read.
        restarted = names[: len(self._segments)] != self._segments or any(changed(n) for n in self._segments)
        if restarted:
            self._reset()
        added: list[StoreLine] = []
        for name in names:
            path = self._root / "segments" / name
            st = path.stat()
            if name not in self._consumed:
                self._consumed[name] = 0
                self._identity[name] = (st.st_dev, st.st_ino)
                self._segments.append(name)
            size = st.st_size
            if size == self._consumed[name]:
                continue
            with open(path, "rb") as fh:
                fh.seek(self._consumed[name])
                data = fh.read(size - self._consumed[name])
            # Complete lines only: a store's index admits complete lines, and
            # a closed segment never ends in a torn one.
            complete = data.rfind(b"\n") + 1
            if name != active and complete != len(data):
                raise LedgerLayoutError(f"{path}: closed segment ends in an unterminated line")
            offset = self._consumed[name]
            for raw in data[:complete].split(b"\n")[:-1]:
                line = StoreLine(name, offset, raw)
                self._tail[name] = (offset, raw + b"\n")
                offset += len(raw) + 1
                if not raw.strip():
                    continue
                capsule_id = _line_id(raw)
                if capsule_id is not None and capsule_id in self._seen:
                    # Not an entry: a duplicate append left it before CLL
                    # checked first. Reported like a flat ledger's corrupt line.
                    logging.getLogger(__name__).warning(
                        "ledger store: skipping a later line repeating capsule_id %s in %s", capsule_id, path
                    )
                    self._skipped.append(line)
                    continue
                if capsule_id is not None:
                    self._seen.add(capsule_id)
                self._entries.append(line)
                added.append(line)
            self._consumed[name] = offset
        return restarted, added


def _line_id(raw: bytes) -> str | None:
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    capsule_id = obj.get("capsule_id") if isinstance(obj, dict) else None
    return capsule_id if isinstance(capsule_id, str) else None


def read_entries(root: str | os.PathLike) -> list[dict]:
    """Every entry of a managed store as a dict, in order, for list readers.
    An archived segment becomes one ``archived_segment`` placeholder (see
    ``capsule_emit.ledger.ARCHIVED_SEGMENT_KIND``), and the readable entries
    after it follow; their leaf positions are unknown. Unparseable lines are
    skipped, as for a flat file."""
    from .ledger import ARCHIVED_SEGMENT_KIND

    root = Path(root)
    segments, active = _read_manifest(root)
    entries: list[dict] = []
    seen: set[str] = set()
    for segment in segments:
        if not segment.mounted:
            entries.append(_archived_placeholder(root, segment, ARCHIVED_SEGMENT_KIND))
            continue
        data = (root / "segments" / segment.name).read_bytes()
        complete = data.rfind(b"\n") + 1
        if segment.name != active and complete != len(data):
            raise LedgerLayoutError(f"{root / 'segments' / segment.name}: closed segment ends in an unterminated line")
        for raw in data[:complete].split(b"\n"):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            capsule_id = obj.get("capsule_id") if isinstance(obj, dict) else None
            if isinstance(capsule_id, str):
                if capsule_id in seen:
                    # Not an entry: a duplicate append left it before CLL
                    # checked first. Reported like a flat ledger's corrupt line.
                    logging.getLogger(__name__).warning(
                        "ledger store: skipping a later line repeating capsule_id %s in %s", capsule_id, segment.name
                    )
                    continue
                seen.add(capsule_id)
            entries.append(obj)
    return entries


def _archived_placeholder(root: Path, segment: _Segment, kind: str) -> dict:
    """What a list reader shows for an archived segment: its range and
    checkpoint from the segment's closing manifest, when it has one."""
    placeholder = {"kind": kind, "segment": segment.name}
    closing = segment.entry.get("manifest")
    if isinstance(closing, str):
        try:
            doc = json.loads((root / closing).read_text(encoding="utf-8"))
            for key in ("first_seq", "last_seq", "record_count"):
                placeholder[key] = doc[key]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for key in ("checkpoint_root", "mmr_size"):
        if key in segment.entry:
            placeholder[key] = segment.entry[key]
    placeholder["note"] = "archived -- mount to view"
    return placeholder


def publish_new_file(path: Path, data: bytes, mode: int = 0o600) -> bool:
    """Write ``data`` as the new file ``path``, complete and fsynced before
    anyone can open it, without replacing a file: a temp file beside it,
    then a hard link into place, which fails if ``path`` exists. On a disk
    without hard links (FAT, exFAT, some network mounts) the temp file is
    renamed into place once ``path`` is seen absent; a file another process
    creates in the instant between the check and the rename is replaced, as
    before capsule-emit 0.13 on every disk. Returns False, writing nothing,
    when ``path`` already exists."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no hard links on this disk
        if os.path.lexists(path):
            return False
        os.rename(tmp, path)
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)


def _fsync(path: Path) -> None:
    """fsync a file or a directory (its entries)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def _open(root: Path):
    from cll.ledger import LedgerStore

    if not hasattr(LedgerStore, "recover"):  # pragma: no cover - dependency floor guards this
        raise LedgerLayoutError("writing a ledger store needs checkpointed-local-log with LedgerStore.recover()")
    return LedgerStore(root, rotate_at_checkpoint=True, max_segment_bytes=sys.maxsize)


def _new_staging(root: Path) -> Path:
    """A fresh staging directory beside ``root``, created exclusively with
    mode 0o777 so the umask applies (never another ledger's directory)."""
    while True:
        staging = root.parent / f".{root.name}.staging-{secrets.token_hex(8)}"
        try:
            os.mkdir(staging, 0o777)
            return staging
        except FileExistsError:
            continue


def create_store(root: Path, first_line: str | None = None, *, lines: list[str] | None = None) -> None:
    """Create a marked store at ``root`` (absent, or an empty directory),
    holding ``first_line`` when given, or every line of ``lines`` in order.
    Built in a fresh staging directory beside ``root`` and renamed into place after everything in it is fsynced.
    The caller holds the writer lock."""
    if root.exists() and not (root.is_dir() and not any(root.iterdir())):
        raise LedgerLayoutError(f"{root} exists and is not an empty directory")
    if root.exists() and os.path.ismount(root):
        # The store is renamed into place, which a mount point cannot take.
        raise LedgerLayoutError(f"{root} is a mount point; keep the ledger in a directory inside it")
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = _new_staging(root)
    try:
        # The store replaces an empty directory the caller made: keep its mode
        # and group (setgid included). A new one has the umask's default.
        if root.is_dir():
            st = root.stat()
            # Group first: changing a directory's group can clear its setgid bit.
            # A group that cannot be kept refuses the write: the mode alone
            # would grant its access to another group.
            if staging.stat().st_gid != st.st_gid:
                try:
                    os.chown(staging, -1, st.st_gid)
                except OSError as exc:
                    raise LedgerLayoutError(
                        f"{root}: cannot give the new store the directory's group ({exc}); "
                        "create the store as a member of that group"
                    ) from exc
            os.chmod(staging, stat.S_IMODE(st.st_mode))
        marker = staging / MARKER_FILENAME
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump(_MARKER, fh)
            fh.flush()
            os.fsync(fh.fileno())
        store = _open(staging)
        try:
            if first_line is not None:
                store.append(json.loads(first_line), consequential=True)
            # Imported lines are history, not new actions (CLL's own import
            # marks them so); every file is fsynced before the rename anyway.
            for text in lines or []:
                store.append(json.loads(text), consequential=False)
        finally:
            store.close()
        for path in sorted(staging.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            _fsync(path)
        _fsync(staging)
        os.rename(staging, root)
        _fsync(root.parent)
    except BaseException:
        if staging.exists():
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
        raise


def append_line(root: Path, text: str) -> None:
    """Append one line to the marked store at ``root`` (created when absent or
    an empty directory). The caller holds the writer lock, has run
    :func:`reconcile` under it and has checked that ``capsule_id`` is new."""
    if not (root / MANIFEST_FILENAME).is_file():
        create_store(root, text)
        return
    if not is_marked(root):
        raise LedgerLayoutError(
            f"{root} is a ledger store capsule-emit did not create ({MARKER_FILENAME} absent); "
            "it is read, never written"
        )
    store = _open(root)
    try:
        store.append(json.loads(text), consequential=True)
    finally:
        store.close()


def reconcile(root: Path) -> None:
    """Run the store's crash recovery (CLL ``recover()``) on a marked store, so
    an append that finds its line already present also finds it indexed. The
    caller holds the writer lock. Does nothing for an absent or foreign
    store."""
    if not (root / MANIFEST_FILENAME).is_file() or not is_marked(root):
        return
    # Validated before CLL opens it: opening a store as its writer can
    # rewrite a manifest it does not like.
    _read_manifest(root)
    store = _open(root)
    try:
        store.recover()
    finally:
        store.close()
