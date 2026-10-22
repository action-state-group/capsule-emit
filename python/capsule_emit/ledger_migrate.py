# SPDX-License-Identifier: Apache-2.0
"""Move a flat ``ledger.jsonl`` into a ledger store, and write a store back out
as plain JSON lines: ``capsule-emit ledger import`` and ``ledger export``.

**Import** copies every line of the flat file, in order and unchanged as JSON,
into a new store (``capsule_emit.ledger_store``), so the store holds the same
leaves: every checkpoint the old file recorded covers the same leaf count and
root over the store's prefix, and its stamps verify there. It is one shot and
all or nothing: the destination must be new (a non-existent name without a
dot, or an empty directory), the store is built in a staging directory and
renamed into place, and nothing is written when the source cannot be copied
whole. A line that does not parse (a fragment a crashed writer left) is not a
leaf, so it is skipped and reported. A line without a 64-hex ``capsule_id``, or
a ``capsule_id`` that repeats, refuses the import: a store admits one line per
``capsule_id``, so a repeat would shift every later leaf. Witnessing starts a
new log, because a ledger's log id is its path. The producer key is carried
over: the old ledger's default key is copied beside the store when the store
has none. The source is never modified and stays readable.

**Export** writes a store's entries, in order and as stored, the line form a
flat ledger holds. Importing an export gives the same lines and roots.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger as _ledger
from . import ledger_store as _store
from .legacy_ledger import _line_form

__all__ = ["ImportRefused", "ImportReport", "export_ledger", "import_ledger", "suggested_store"]

_HEX64 = re.compile(r"[0-9a-f]{64}")


class ImportRefused(ValueError):
    """The flat ledger cannot be copied whole, so no store was created.
    ``lines`` names the 1-based source lines at fault. A producer key copied
    beside the store before the refusal stays: it is the same key."""

    def __init__(self, message: str, lines: list[int]) -> None:
        super().__init__(message)
        self.lines = lines


@dataclass
class ImportReport:
    source: Path
    store: Path
    imported: Counter = field(default_factory=Counter)
    skipped_unparseable: list[int] = field(default_factory=list)
    reserialized: list[int] = field(default_factory=list)
    key_copied: Path | None = None
    #: The store already had a key (``store_key``) that is not the old ledger's.
    key_differs: bool = False
    store_key: Path | None = None
    replaced_utf8: list[int] = field(default_factory=list)
    #: The key seal() to this store will sign with, and why.
    key_in_use: Path | None = None
    key_source: str = ""


def _kind(obj: dict) -> str:
    if obj.get("kind"):
        return str(obj["kind"])
    if obj.get("record_type") == _ledger.PADDING_RECORD_TYPE:
        return "padding"
    return "capsule"


def _source_key(source: Path) -> Path:
    from .signing import _default_key_path

    return _default_key_path(source)


def _store_key(root: Path) -> Path:
    return Path(os.fspath(root) + ".signing_key.pem")


def suggested_store(path: str | os.PathLike) -> str:
    """The store directory the deprecation messages print for a flat ledger:
    ``./capsule-ledger`` for ``./ledger.jsonl``; otherwise, beside the file,
    its name with leading dots dropped, each other ``.`` replaced by ``-`` and
    ``-store`` added. If that path exists and is not an empty directory, the
    first free ``-2``, ``-3``, ... suffix. Always printed with a directory
    prefix, so it never reads as a command-line option."""
    p = Path(path)
    parent = p.parent
    if p.name == "ledger.jsonl" and os.path.realpath(parent) == os.path.realpath(os.curdir):
        base = "capsule-ledger"
    else:
        base = p.name.lstrip(".").replace(".", "-") + "-store"
    def taken(candidate: Path) -> bool:
        # A leftover key beside a name would become the new store's identity.
        return (candidate.exists() and not (candidate.is_dir() and not any(candidate.iterdir()))) or _store_key(
            candidate
        ).exists()

    candidate = parent / base
    n = 2
    while taken(candidate):
        candidate = parent / f"{base}-{n}"
        n += 1
    text = os.fspath(candidate)
    if not os.path.dirname(text) or text.startswith("-"):
        text = os.path.join(os.curdir, text)
    return text


def _check_destination(dest: Path) -> None:
    root = _store.store_root(dest)
    for parent in (dest.parent, root.parent):
        if not parent.is_dir():
            raise ImportRefused(f"{parent} does not exist; create it first", [])
    if dest.exists():
        if not dest.is_dir() or any(dest.iterdir()):
            raise ImportRefused(f"{dest} exists and is not an empty directory; choose a new name", [])
    elif "." in dest.name:
        raise ImportRefused(f"{dest}: a new ledger store's name has no '.'; choose a name without one", [])


def _publish_key(source_key: Path, dest_key: Path) -> None:
    """Copy ``source_key`` to ``dest_key`` without ever leaving a partial key
    and without ever replacing one: an exclusive temp file beside it, fsynced,
    then hard-linked into place, which fails if a key appeared meanwhile (a
    ``seal()`` that created one is signing with it)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{dest_key.name}.", dir=dest_key.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(source_key.read_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, dest_key)
        except FileExistsError:
            raise ImportRefused(
                f"a producer key appeared at {dest_key} during the import, so it was not replaced; "
                "nothing was imported. Check which key that ledger should use, then import again.",
                [],
            ) from None
        _store._fsync(dest_key.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _same_file(a: Path, b: Path) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _key_in_use(dest_key: Path) -> tuple[Path | None, str]:
    """The key seal() to the new store will sign with, and how it was chosen."""
    from .signing import SIGNING_KEY_PATH_ENV_VAR

    override = os.environ.get(SIGNING_KEY_PATH_ENV_VAR)
    if override:
        return Path(override), SIGNING_KEY_PATH_ENV_VAR
    if dest_key.exists():
        return dest_key, "default"
    return None, "new"


def _cp_command(source_key: Path, dest_key: Path) -> str:
    return f"cp -n -- {shlex.quote(os.fspath(source_key))} {shlex.quote(os.fspath(dest_key))}"


def import_ledger(source: str | os.PathLike, dest: str | os.PathLike) -> ImportReport:
    """Copy the flat ledger ``source`` into a new ledger store ``dest``.
    Raises :class:`ImportRefused` (nothing written) when it cannot be copied
    whole; see the module docstring."""
    source = Path(source)
    if not source.is_file():
        raise ImportRefused(f"{source} is not a flat ledger file", [])
    dest = Path(dest)
    _check_destination(dest)
    root = _store.store_root(dest)
    # Taking the locks rewrites the lock files: a source that is one of them
    # (by name, symlink or hard link) would be destroyed before it is read.
    for lock in (Path(os.fspath(root) + ".lock"), Path(os.fspath(source) + ".lock")):
        if _same_file(source, lock):
            raise ImportRefused(f"{source} is the lock file {lock}; it is not a ledger", [])
    report = ImportReport(source=source, store=root)
    with _ledger._writer_lock(root, wait=True), _ledger._writer_lock(source, wait=True):
        _check_destination(dest)
        texts: list[str] = []
        seen: dict[str, int] = {}
        bad_ids: list[int] = []
        repeats: list[int] = []
        with open(source, "rb") as fh:
            for number, raw in enumerate(fh, start=1):
                # Decoded, then stripped, exactly as the flat reader reads it
                # (LegacyLedger._parse), so a line it counts as a leaf is a
                # leaf here too.
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    report.skipped_unparseable.append(number)
                    continue
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    report.replaced_utf8.append(number)
                if not isinstance(obj, dict) or not isinstance(obj.get("capsule_id"), str) or not _HEX64.fullmatch(obj["capsule_id"]):
                    bad_ids.append(number)
                    continue
                if obj["capsule_id"] in seen:
                    repeats.append(number)
                    continue
                seen[obj["capsule_id"]] = number
                line = _line_form(obj)
                if line.encode() != raw.strip() and number not in report.replaced_utf8:
                    report.reserialized.append(number)
                texts.append(line)
                report.imported[_kind(obj)] += 1
        if bad_ids:
            raise ImportRefused(
                f"{source}: line(s) {_lines(bad_ids)} have no lowercase 64-hex capsule_id, which a ledger store "
                "needs; nothing was imported",
                bad_ids,
            )
        if repeats:
            message = (
                f"{source}: line(s) {_lines(repeats)} repeat a capsule_id already on an earlier line, "
                "which a ledger store admits only once (likely repeated hold lines, which capsule-emit "
                "wrote before it checked for repeats); nothing was imported. Keep the file for reading "
                "and verifying, and seal new records to a new ledger directory."
            )
            source_key, dest_key = _source_key(source), _store_key(root)
            if source_key.is_file() and not dest_key.exists():
                message += (
                    " To keep the producer identity, run this before the first seal to the new directory: "
                    + _cp_command(source_key, dest_key)
                )
            raise ImportRefused(message, repeats)

        source_key, dest_key = _source_key(source), _store_key(root)
        if source_key.is_file() and not dest_key.exists():
            _publish_key(source_key, dest_key)
            report.key_copied = dest_key
        elif source_key.is_file() and dest_key.is_file():
            report.key_differs = source_key.read_bytes() != dest_key.read_bytes()
        report.store_key = dest_key
        report.key_in_use, report.key_source = _key_in_use(dest_key)
        try:
            _store.create_store(root, lines=texts)
        except ValueError as exc:
            raise ImportRefused(f"{source}: a line cannot be stored ({exc}); nothing was imported", []) from exc
    return report


def format_lines(numbers: list[int]) -> str:
    """Line numbers for a message: the first ten, then a count."""
    return _lines(numbers)


def _lines(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:10])
    return shown + (f" and {len(numbers) - 10} more" if len(numbers) > 10 else "")


@dataclass
class ExportReport:
    lines: int = 0
    skipped_repeats: int = 0


def export_ledger(store: str | os.PathLike, out) -> ExportReport:
    """Write the entries of ``store`` to the binary stream ``out`` as JSON
    lines, in order and as stored. Raises
    ``ledger_store.LedgerArchivedError`` when a segment is archived."""
    root = Path(store)
    if not _ledger.is_ledger_store(root):
        raise _store.LedgerLayoutError(f"{root} is not a ledger store directory")
    lines = _store.StoreLines(root)
    _, entries = lines.refresh()
    report = ExportReport(skipped_repeats=len(lines.skipped))
    for line in entries:
        out.write(line.raw + b"\n")
        report.lines += 1
    return report


def export_to_file(store: str | os.PathLike, target: str | os.PathLike) -> ExportReport:
    """Export to the file ``target``: written to a temp file beside it (with
    the usual file mode), fsynced, then renamed over it, so a failed export
    never leaves a partial file. Refuses a directory target and one inside the
    store."""
    root, target = _store.store_root(store), Path(target)
    if target.is_dir():
        raise _store.LedgerLayoutError(f"{target} is a directory; name a file")
    # The entry replaced is the target's own path (os.replace does not follow a
    # symlink), and what it may point at: both are checked against the store
    # and its key and lock, by name and by file.
    store_paths = {Path(store).absolute(), root}
    sidecars = [Path(os.fspath(p) + suffix) for p in store_paths for suffix in (".signing_key.pem", ".lock")]
    entry, real = Path(os.path.abspath(target)), Path(os.path.realpath(target))
    inside = any(c.is_relative_to(p) for c in (entry, real) for p in store_paths)
    if inside or any(c in sidecars or _same_file(c, sc) for c in (entry, real) for sc in sidecars if sc.exists()):
        raise _store.LedgerLayoutError(f"{target} is part of the store {root} (or its key or lock); write the export elsewhere")
    while True:
        tmp = target.parent / f".{target.name}.{secrets.token_hex(8)}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as fh:
            report = export_ledger(root, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        _store._fsync(target.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return report
