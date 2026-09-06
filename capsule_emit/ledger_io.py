# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing every ledger-backed read verb (log/show/verify/bundle/agents)
across the workspace uses: opening a ledger from a CLI argument, the common
filter-flag set, and the ``ScanQuery`` it builds.

**Moved here from ``capsule_ledger.cli.ledger_io`` / the public
``capsule_ledger.io`` re-export** ([emit-ledger-io-home], 2026-09-06).
``capsule-ledger`` was archived (read-only) on 2026-09-02; this module is the
read/verify seam every ledger-backed verb depends on, so it needed a home
that stays maintained.

``LedgerStore``/``ScanQuery`` come from :mod:`cll.ledger` -- the
``checkpointed-local-log`` package -- not ``capsule_ledger``: the W3.1 CLL
extraction (2026-09-01) already made ``capsule_ledger.ledger.store``/
``capsule_ledger.ledger.api`` thin ``sys.modules`` aliases for
``cll.ledger.store``/``cll.ledger.api``, and ``cll`` is already this
package's own hard, unconditional dependency (``checkpointed-local-log``),
so importing the *actual*, still-maintained module directly needs no new
dependency and no Python-floor gate (Amendment E: import, never vendor --
and importing the alias's target rather than the archived alias itself is
the more honest "import").

``PayloadStore`` has no ``cll`` equivalent -- it stays ``capsule_ledger``'s
own class, so :func:`local_payload_store` imports it lazily, on call, from
the optional ``capsule-emit[ledger-io]`` extra. ``capsule-ledger`` requires
Python >=3.10, so that extra (and only that extra/function) is unusable on
this package's 3.9 floor; every other function in this module works
unconditionally.

``--ledger`` accepts either a real ``LedgerStore`` root (a directory) or a
plain JSONL fixture file (imported into an ephemeral, throwaway store for
the duration of the command) -- the same convenience ``fold test`` already
offers via its own ``--ledger`` flag, extended here to every query-API verb
so fixture ledgers work directly with no separate "build a store" step.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from cll.ledger import LedgerStore, ScanQuery

if TYPE_CHECKING:
    from capsule_ledger.payload_store import PayloadStore

__all__ = [
    "open_ledger",
    "require_ledger_path",
    "add_scan_query_args",
    "build_scan_query",
    "echo_parts",
    "local_payload_store",
]


@contextlib.contextmanager
def open_ledger(path: str | os.PathLike) -> Iterator[LedgerStore]:
    p = Path(path)
    if p.is_dir():
        store = LedgerStore(p)
        try:
            yield store
        finally:
            store.close()
        return

    tmp_root = Path(tempfile.mkdtemp(prefix="capsule-ledger-cli-"))
    store = LedgerStore(tmp_root)
    try:
        store.import_jsonl(p)
        yield store
    finally:
        store.close()
        shutil.rmtree(tmp_root, ignore_errors=True)


def local_payload_store(ledger_path: str | os.PathLike) -> PayloadStore | None:
    """The resolve-at-read gate (item 5a): auto-resolve applies only on a
    LOCAL, standalone-grade ledger with a payload store actually present --
    never on an imported JSONL fixture or a foreign bundle, which
    ``open_ledger()`` opens into a throwaway tempdir with no lasting home
    for one. Returns ``None`` (never a store you'd have to remember to
    check ``.exists`` on) unless both conditions hold.

    Requires the optional ``capsule-emit[ledger-io]`` extra (Python >=3.10)
    -- imported lazily here so the rest of this module stays usable without
    it."""
    root = Path(ledger_path)
    if not root.is_dir():
        return None
    from capsule_ledger.payload_store import PayloadStore

    store = PayloadStore(root)
    return store if store.exists else None


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
