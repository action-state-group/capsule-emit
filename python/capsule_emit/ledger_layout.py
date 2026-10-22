# SPDX-License-Identifier: Apache-2.0
"""Which ledger a call writes, and the deprecation of the flat ``ledger.jsonl``.

capsule-emit's default ledger is a ledger store directory, ``./capsule-ledger/``
(checkpointed-local-log's managed ``LedgerStore``; see ``capsule_emit.ledger_store``).
A flat ``ledger.jsonl`` is still read forever, imported with ``capsule-emit ledger
import``, and written during a deprecation window. See capsule-emit#358.

**Choosing the ledger.** ``ledger=`` when given; else ``CAPSULE_LEDGER``; else the
default. A chosen path resolves, in order, to:

- a ledger store: a directory holding ``manifest.json``, an empty directory, or a
  path that does not exist and whose name has no ``.``;
- a flat ledger: an existing regular file;
- refused (:class:`~capsule_emit.ledger_store.LedgerLayoutError`): any other
  directory, any other existing path, a non-existent name with a ``.``, and,
  from this release on, a non-existent ``.jsonl`` path, because no new flat
  ledger is created (:class:`FlatLedgerWriteRefused`).

The default, with nothing chosen: ``./capsule-ledger/`` if it exists (refused if
it is not a store or an empty directory); else ``./ledger.jsonl`` if it is a file,
written during the window; else a new ``./capsule-ledger/``. The default is
resolved on every call.

**The window.** New records written to a flat ledger (``seal()``, ``log()``,
``received()``, holds, and the helpers built on them) emit
:class:`LedgerDeprecationWarning` once per process per path, and from
:data:`FLAT_WRITES_STOP` on raise :class:`FlatLedgerWriteRefused`. Lines that act on
evidence a flat ledger already holds (checkpoint stamps, witness backfills,
disclosure records) never warn and never stop, and reading never ends.
"""
from __future__ import annotations

import os
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

from .ledger_store import MANIFEST_FILENAME, LedgerLayoutError, is_store_path, is_store_target

__all__ = [
    "DEFAULT_FLAT",
    "DEFAULT_STORE",
    "DefaultMoved",
    "FLAT_WRITES_STOP",
    "LEDGER_ENV",
    "FlatLedgerWriteRefused",
    "LedgerDeprecationWarning",
    "Resolution",
    "check_new_flat_record",
    "is_store_target",
    "resolve_ledger",
]

DEFAULT_STORE = "capsule-ledger"
DEFAULT_FLAT = "ledger.jsonl"
LEDGER_ENV = "CAPSULE_LEDGER"

#: The release from which new records are no longer written to a flat ledger:
#: the second minor release after the one that makes the store the default
#: (N = 0.13.0, so N + 2 = 0.15.0). Compared with the installed version.
FLAT_WRITES_STOP = "0.15.0"

# The test suite's hooks (tests/conftest.py): its flat-ledger tests create
# fresh flat ledgers and would warn on every one; the rule and the warning are
# tested on their own (tests/test_ledger_layout.py).
_allow_new_flat = False
_warn_flat_writes = True


class LedgerDeprecationWarning(FutureWarning):
    """Writing new records to a flat ``ledger.jsonl`` is deprecated. A
    ``FutureWarning``, so it is shown by default."""


class FlatLedgerWriteRefused(LedgerLayoutError):
    """A new record, or a new flat ledger, was refused: the flat ledger is
    retired for writing. The message names the import command."""


class DefaultMoved(Exception):
    """Internal: the default moved to ``./capsule-ledger/`` while a call that
    resolved to ``./ledger.jsonl`` waited for the flat file's lock; the call
    wrote nothing and runs again."""


@dataclass(frozen=True)
class Resolution:
    """Where a call writes. ``chosen`` is how the path was picked:
    ``"argument"``, the environment variable's name, or ``"default"``."""

    path: Path
    layout: str  # "store" or "flat"
    chosen: str

    @property
    def default_flat(self) -> bool:
        """The default fell back to an existing ``./ledger.jsonl``."""
        return self.chosen == "default" and self.layout == "flat"


def _classify(path: Path, chosen: str) -> Resolution:
    if path.is_dir():
        if is_store_path(path):
            return Resolution(path, "store", chosen)
        what = "an unmanaged store or another tool's directory" if (path / "segments").is_dir() else "not a ledger store"
        raise LedgerLayoutError(f"{path} is a directory that is {what}, and not empty")
    if path.is_file():
        return Resolution(path, "flat", chosen)
    if path.exists():
        raise LedgerLayoutError(f"{path} is not a file or a directory")
    name = Path(os.path.normpath(os.fspath(path))).name
    if name.endswith(".jsonl"):
        if _allow_new_flat:
            return Resolution(path, "flat", chosen)
        from .ledger_migrate import suggested_store

        raise FlatLedgerWriteRefused(
            f"{path} does not exist, and capsule-emit no longer creates flat ledgers. "
            f'Seal to a ledger directory instead, for example ledger="{suggested_store(path)}" '
            "(or leave ledger= unset to use the default, ./capsule-ledger/)."
        )
    if "." in name:
        raise LedgerLayoutError(
            f"{path}: a new ledger directory's name has no '.'; create the directory first, or choose another name"
        )
    return Resolution(path, "store", chosen)


def resolve_ledger(ledger: str | os.PathLike | None = None, *, env: tuple[str, ...] = (LEDGER_ENV,)) -> Resolution:
    """The ledger a call writes: ``ledger`` when given, else the first of
    ``env`` that is set, else the default (see the module docstring)."""
    if ledger is not None:
        return _classify(Path(ledger), "argument")
    for name in env:
        value = os.environ.get(name)
        if value:
            return _classify(Path(value), name)
    store = Path(DEFAULT_STORE)
    if store.exists():
        if store.is_dir() and is_store_path(store):
            return Resolution(store, "store", "default")
        raise LedgerLayoutError(
            f"./{DEFAULT_STORE} exists and is not a ledger store or an empty directory; "
            "pass ledger= to choose a ledger"
        )
    flat = Path(DEFAULT_FLAT)
    if flat.is_file():
        return Resolution(flat, "flat", "default")
    return Resolution(store, "store", "default")


def default_moved(resolution: Resolution) -> bool:
    """Whether a default that fell back to ``./ledger.jsonl`` would now resolve
    elsewhere (an import published ``./capsule-ledger/`` meanwhile)."""
    return resolution.default_flat and (Path(DEFAULT_STORE) / MANIFEST_FILENAME).is_file()


def _installed_version() -> tuple[int, ...] | None:
    from . import __version__

    try:
        return tuple(int(part) for part in __version__.split("+")[0].split(".")[:3])
    except ValueError:
        return None


def flat_writes_stopped() -> bool:
    installed = _installed_version()
    stop = tuple(int(part) for part in FLAT_WRITES_STOP.split("."))
    return installed is not None and installed >= stop


_warned: set[str] = set()
_warned_lock = threading.Lock()


def _message(path: Path, chosen: str) -> str:
    from .ledger_migrate import suggested_store

    target = suggested_store(path)
    if chosen == "default":
        unset = " (or leave ledger= unset to use the default)"
    elif chosen == "argument":
        unset = ""
    else:
        unset = f" and set {chosen}={target}"
    return (
        f"capsule-emit: new records to the flat ledger {path} stop in capsule-emit {FLAT_WRITES_STOP}. "
        f'To move it: capsule-emit ledger import {path} {target}, then seal with ledger="{target}"{unset}. '
        f"Reading {path} keeps working."
    )


def check_new_flat_record(path: str | os.PathLike, chosen: str = "argument") -> None:
    """Called before a new record is written to the flat ledger ``path``:
    warns once per process per path during the window, and from
    :data:`FLAT_WRITES_STOP` on raises :class:`FlatLedgerWriteRefused`."""
    path = Path(path)
    message = _message(path, chosen)
    if flat_writes_stopped():
        raise FlatLedgerWriteRefused(message)
    if not _warn_flat_writes:
        return
    key = os.path.abspath(os.fspath(path))
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    warnings.warn(message, LedgerDeprecationWarning, stacklevel=3)
