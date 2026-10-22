# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing every ledger-backed read verb (log/show/verify/bundle/agents)
across the workspace uses: opening a ledger from a CLI argument, the common
filter-flag set, and the ``ScanQuery`` it builds.

**Moved here from ``capsule_ledger.cli.ledger_io`` / the public
``capsule_ledger.io`` re-export** (2026-09-06).
``capsule-ledger`` was archived (read-only) on 2026-09-02; this module is the
read/verify seam every ledger-backed verb depends on, so it needed a home
that stays maintained.

``LedgerStore``/``ScanQuery`` come from :mod:`cll.ledger` -- the
``checkpointed-local-log`` package -- not ``capsule_ledger``: the CLL
extraction (2026-09-01) already made ``capsule_ledger.ledger.store``/
``capsule_ledger.ledger.api`` thin ``sys.modules`` aliases for
``cll.ledger.store``/``cll.ledger.api``, and ``cll`` is already this
package's own hard, unconditional dependency (``checkpointed-local-log``),
so importing the *actual*, still-maintained module directly needs no new
dependency and no Python-floor gate (import, never vendor --
and importing the alias's target rather than the archived alias itself is
the more honest "import").

``--ledger`` accepts either a real ``LedgerStore`` root (a directory) or a
plain JSONL fixture file (imported into an ephemeral, throwaway store for
the duration of the command) -- the same convenience ``fold test`` already
offers via its own ``--ledger`` flag, extended here to every query-API verb
so fixture ledgers work directly with no separate "build a store" step.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from cll.ledger import LedgerStore, ScanQuery

__all__ = [
    "open_ledger",
    "require_ledger_path",
    "add_scan_query_args",
    "build_scan_query",
    "echo_parts",
]


def _stored_flags(root: Path) -> dict[str, bool]:
    """Each record's ``consequential`` flag from a managed store's index. The
    index and its WAL are copied into a temporary directory and read there, so
    the store is never touched (no lock, no sidecar) and rows a writer has
    committed only to the WAL are still seen; SQLite's WAL checksums drop a
    frame copied mid-write. Empty when it cannot be read (the copy then marks
    records consequential, as ``seal()`` appends them)."""
    root = Path(os.path.abspath(root))
    with tempfile.TemporaryDirectory(prefix="capsule-ledger-index-") as tmp:
        try:
            for name in ("index.sqlite3", "index.sqlite3-wal"):
                if (root / name).is_file():
                    shutil.copyfile(root / name, Path(tmp) / name)
            conn = sqlite3.connect(Path(tmp) / "index.sqlite3")
            try:
                return {cid: bool(flag) for cid, flag in conn.execute("SELECT capsule_id, consequential FROM records")}
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return {}


def _copy_store(source: Path, target: LedgerStore) -> None:
    """Copy a managed store's entries, read from its segments (crash-left
    lines included), into ``target``, keeping what the scan verbs query:
    authenticity (re-derived from each line's envelope, as CLL's ``reindex``
    does), the ``consequential`` flag and the correlation fields."""
    from cll.ledger import SIGNED, AdmissionRequest, ProducerEnvelope

    from .ledger import read_positioned_entries

    flags = _stored_flags(source)
    for entry in read_positioned_entries(source):
        admission = None
        if LedgerStore._reindex_authenticity(entry) == "signed":
            admission = AdmissionRequest(
                mode=SIGNED, envelopes=(ProducerEnvelope(envelope=entry["signature"], key_id=entry["key_id"]),)
            )
            entry = {k: v for k, v in entry.items() if k not in ("signature", "key_id")}
        target.append(entry, consequential=flags.get(entry.get("capsule_id"), True), admission=admission)


def _correlation_fields(source: Path) -> tuple[str, ...]:
    try:
        return tuple(json.loads((source / "lookup_config.json").read_text())["correlation_fields"])
    except (OSError, ValueError, KeyError, TypeError):
        return ()


@contextlib.contextmanager
def open_ledger(path: str | os.PathLike) -> Iterator[LedgerStore]:
    """A ``LedgerStore`` over ``path`` for the scan-query verbs. A managed
    store (``manifest.json``, as ``seal()`` writes) is never opened: its
    entries are copied, from the segments, into an ephemeral store with their
    authenticity, ``consequential`` flag and the store's correlation fields,
    so a read writes nothing, works on a read-only copy and sees a line a
    crashed writer left. A store with an archived segment raises
    ``ledger_store.LedgerArchivedError`` (positions after it are unknown). An
    empty directory is an empty ledger and is left empty. An unmanaged store
    directory (no ``manifest.json``) is opened as before, and a JSONL file is
    imported into an ephemeral store, as before."""
    from .ledger_store import MANIFEST_FILENAME

    p = Path(path)
    if p.is_dir() and any(p.iterdir()) and not (p / MANIFEST_FILENAME).is_file():
        store = LedgerStore(p)
        try:
            yield store
        finally:
            store.close()
        return

    tmp_root = Path(tempfile.mkdtemp(prefix="capsule-ledger-cli-"))
    managed = p.is_dir() and (p / MANIFEST_FILENAME).is_file()
    store = LedgerStore(tmp_root, correlation_fields=_correlation_fields(p) if managed else ())
    try:
        if managed:
            _copy_store(p, store)
        elif not p.is_dir():
            store.import_jsonl(p)
        yield store
    finally:
        store.close()
        shutil.rmtree(tmp_root, ignore_errors=True)


def require_ledger_path(verb: str, args: argparse.Namespace) -> str | None:
    """Resolve ``--ledger`` (or ``$CAPSULE_LEDGER``);
    prints a usage error and returns ``None`` rather than raising, so callers
    can return a clean exit code instead of an uncaught ``SystemExit``."""
    path = getattr(args, "ledger", None) or os.environ.get("CAPSULE_LEDGER")
    if path is None:
        print(f"capsule {verb}: --ledger is required (or set $CAPSULE_LEDGER)", file=sys.stderr)
    return path


def add_scan_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", help="ledger store directory or a JSONL fixture file (default: $CAPSULE_LEDGER)")
    parser.add_argument("--agent", help="filter: developer/agent id (ScanQuery.agent)")
    parser.add_argument("--since", help="filter: inclusive lower timestamp bound (ISO-8601)")
    parser.add_argument("--until", help="filter: inclusive upper timestamp bound (ISO-8601)")
    parser.add_argument("--counterparty", help="filter: operator (ScanQuery.counterparty)")
    parser.add_argument("--verdict", help="filter: disposition.verdict_class")
    parser.add_argument("--action-type", dest="action_type", help="filter: action_type")
    parser.add_argument("--limit", type=int, help="filter: maximum records returned")


def build_scan_query(args: argparse.Namespace) -> ScanQuery:
    return ScanQuery(
        agent=args.agent,
        since=args.since,
        until=args.until,
        counterparty=args.counterparty,
        verdict=args.verdict,
        action_type=args.action_type,
        limit=args.limit,
    )


def echo_parts(args: argparse.Namespace) -> list[tuple[str, object]]:
    """The filter flags in their fixed CLI-echo order."""
    return [
        ("--agent", args.agent),
        ("--since", args.since),
        ("--until", args.until),
        ("--counterparty", args.counterparty),
        ("--verdict", args.verdict),
        ("--action-type", args.action_type),
        ("--limit", args.limit),
    ]
