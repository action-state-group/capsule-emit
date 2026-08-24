# SPDX-License-Identifier: Apache-2.0
"""Ledger read/write utilities and the ledger view renderer.

The ledger is a newline-delimited JSON (JSONL) file — one JSON dict per line.
Two kinds of entry share the same file and the same append path:

- **capsule records** — every sealed capsule, unchanged since before this
  module supported a second kind. No ``kind`` field.
- **checkpoint-stamp records** (``kind == CHECKPOINT_STAMP_KIND``, added
  0.5.0 -- see ``capsule_emit.witness``) -- a persisted
  ``capsule_emit.checkpoint.CheckpointRecord`` (with whatever
  ``WitnessRecord`` s it collected), written back into the *same* ledger it
  covers so the stamp becomes a leaf the *next* checkpoint's MMR root
  genuinely commits over: "checkpoint N's stamp is covered by checkpoint
  N+1" (frozen surface §2.3). ``read_ledger`` filters these out by default
  so every existing capsule-only consumer (CLI, server, permalink,
  approval, holds, the view/show renderers below) keeps seeing exactly the
  capsule stream it always has; ``read_ledger_entries`` returns the raw,
  unfiltered file for consumers that must see every leaf (currently only
  ``capsule_emit.witness._JsonlLogSource.scan``, so the MMR indexes stamp
  entries as leaves too).

Four rendering levels (capsule records only):

- ``view()``        — L1 one-line-per-capsule summary table (default)
- ``view_chains()`` — L2 tree grouped by chain.parent_capsule_id
- ``show()``        — L3 full single-capsule two-tier layout
- JSON passthrough  — L4 via CLI ``--json`` flag (not a function here)

Cross-process write safety: one log, one writer (frozen surface §7d) -- see
``docs/concurrency.md``. An OS-level flock on a sidecar ``<ledger>.lock``
file, held only for the duration of one append, gives a second *process* the
same exclusion ``_append_lock`` below already gives a second *thread*. A
second writer finds the lock held and fails immediately with
:class:`LedgerLockedError` naming the holder -- waiting is opt-in
(``append_to_ledger(..., wait=True)``), never silent, because a torn log
manufactures fork evidence.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - CI is Linux-only; documented as a POSIX mechanism.
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "append_to_ledger",
    "read_ledger",
    "read_ledger_entries",
    "CHECKPOINT_STAMP_KIND",
    "LedgerLockedError",
    "view",
    "view_chains",
    "show",
]

#: Marks a ledger line as a persisted checkpoint/witness record rather than
#: a capsule. ``v: 1`` on the entry itself is the format-version marker for
#: this shape (see ``capsule_emit.witness._build_and_register``) -- a future,
#: incompatible stamp-entry shape bumps that number rather than reusing it.
CHECKPOINT_STAMP_KIND = "checkpoint_stamp"

# Physical write safety: two threads calling append_to_ledger for
# *different* files never contend, but two threads appending to the SAME
# ledger file must not interleave their writes. This lock guarantees that —
# it says nothing about business atomicity (e.g. holds/scope.py's per-scope
# reservation lock), which is a caller-level concern layered on top.
_append_lock = threading.Lock()


class LedgerLockedError(OSError):
    """A second writer process found ``append_to_ledger``'s OS-level flock
    already held. See ``docs/concurrency.md`` ("One log, one writer").

    Subclasses ``OSError`` so existing broad ``except OSError`` write-failure
    handling (e.g. ``witness._persist_checkpoint_stamp``'s fire-and-forget
    stamp write) already treats a lock conflict the same as any other write
    failure, without a new except clause."""


def _lock_path(path: Path) -> Path:
    """The sidecar lock file for a ledger: ``<name>.lock`` beside it. flock
    exclusion applies per *open file description*, so a second process that
    opens this same path independently gets its own contending attempt --
    that's what makes this cross-process, not just cross-thread."""
    return path.with_name(path.name + ".lock")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _describe_holder(lock_fh: Any) -> str:
    """Best-effort holder identity from the lock file's contents. Reading is
    never blocked by another process's exclusive flock -- only a competing
    flock attempt is. Degrades to "unknown holder" rather than raising if
    the holder crashed before its first write, or the read races a write."""
    try:
        lock_fh.seek(0)
        raw = lock_fh.read()
        info = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        info = {}
    pid = info.get("pid", "?")
    host = info.get("hostname", "?")
    since = info.get("acquired_at", "?")
    return f"pid {pid} on {host} (writer since {since})"


def _locked_message(path: Path, lock_fh: Any, *, waited: float | None) -> str:
    holder = _describe_holder(lock_fh)
    waited_clause = f" after waiting {waited}s" if waited is not None else ""
    return (
        f"ledger {path} is locked by another writer ({holder}){waited_clause}. "
        "capsule-emit enforces one writer per log -- a torn log manufactures "
        "fork evidence, so a second writer is never allowed to interleave "
        "silently. Route writes through the existing writer, or opt in to "
        "waiting with append_to_ledger(..., wait=True[, timeout=seconds]). "
        'See docs/concurrency.md ("One log, one writer").'
    )


def _acquire_lock(lock_fh: Any, path: Path, *, wait: bool, timeout: float | None) -> None:
    if fcntl is None:  # pragma: no cover - CI is Linux-only.
        raise LedgerLockedError(
            f"cannot lock ledger {path}: flock is a POSIX mechanism and this "
            "platform has no fcntl module. See docs/concurrency.md."
        )

    if not wait:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LedgerLockedError(_locked_message(path, lock_fh, waited=None)) from None
        return

    if timeout is None:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)  # blocking wait, opted in
        return

    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LedgerLockedError(_locked_message(path, lock_fh, waited=timeout)) from None
            time.sleep(0.05)


@contextlib.contextmanager
def _writer_lock(path: Path, *, wait: bool = False, timeout: float | None = None):
    """Hold the cross-process flock for ``path`` for one append. Internal --
    ``append_to_ledger`` is the public entry point; exposed at module level
    only so tests can hold the lock across a real second OS process."""
    lock_fh = open(_lock_path(path), "a+", encoding="utf-8")
    try:
        _acquire_lock(lock_fh, path, wait=wait, timeout=timeout)
        lock_fh.seek(0)
        lock_fh.truncate()
        lock_fh.write(json.dumps({"pid": os.getpid(), "hostname": socket.gethostname(), "acquired_at": _utc_now()}))
        lock_fh.flush()
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()


def append_to_ledger(
    capsule: dict,
    path: str | os.PathLike = "ledger.jsonl",
    *,
    wait: bool = False,
    timeout: float | None = None,
) -> int:
    """Append a sealed capsule dict as a single JSON line.

    Enforces one log, one writer (docs/concurrency.md): a second process
    already writing this same ledger raises :class:`LedgerLockedError`
    immediately, naming the holder. Pass ``wait=True`` (optionally with a
    ``timeout`` in seconds) to block instead of failing -- opt-in only, never
    the default, so a stalled writer never silently queues callers.

    Returns the entry's 1-indexed sequence position within this ledger
    file -- the same ``seq`` ``_JsonlLogSource.scan`` (``capsule_emit.witness``)
    assigns per raw line, checkpoint-stamp entries counted too, i.e. the MMR
    leaf position this entry occupies once a checkpoint covers it. Computed
    under the same lock that serializes the write, so it reflects this
    process's own append order; most callers don't need it and ignore it.
    """
    p = Path(path)
    with _append_lock, _writer_lock(p, wait=wait, timeout=timeout):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(capsule, separators=(",", ":")) + "\n")
        return len(read_ledger_entries(path))


def read_ledger_entries(path: str | os.PathLike) -> list[dict]:
    """Read every line of a JSONL ledger file, capsules and checkpoint-stamp
    records alike, in append order.

    Corrupt lines (truncated writes, disk errors) are skipped with a warning so
    that one bad line never makes the entire ledger unreadable.
    """
    import logging

    p = Path(path)
    if not p.exists():
        return []
    records = []
    with open(p, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logging.getLogger(__name__).warning(
                    "read_ledger: skipping corrupt line %d in %s: %r", lineno, p, line[:80]
                )
    return records


def read_ledger(path: str | os.PathLike) -> list[dict]:
    """Read all capsule records from a JSONL ledger file.

    Checkpoint-stamp records (``kind == CHECKPOINT_STAMP_KIND``) are excluded
    -- this is the capsule-only view every existing consumer expects. Use
    ``read_ledger_entries`` to see the raw file, stamps included.
    """
    return [r for r in read_ledger_entries(path) if r.get("kind") != CHECKPOINT_STAMP_KIND]


# ---------------------------------------------------------------------------
# L1 — flat summary table
# ---------------------------------------------------------------------------

def view(path: str | os.PathLike, *, out: Any = None) -> None:
    """L1: one-line-per-capsule summary table.

    Args:
        path: Path to the JSONL ledger file.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    if not records:
        print(f"ledger: {path} — empty or not found", file=out)
        return

    col_id = 14
    col_action = 22
    col_op = 14
    col_effect = 22
    col_verdict = 12

    header = (
        f"{'capsule_id':<{col_id}}  "
        f"{'action':<{col_action}}  "
        f"{'operator':<{col_op}}  "
        f"{'effect/status':<{col_effect}}  "
        f"{'verdict':<{col_verdict}}  "
        f"{'chain'}"
    )
    print(f"\ncapsule-emit ledger: {path}  ({len(records)} record(s))\n", file=out)
    print(header, file=out)
    print("-" * len(header), file=out)

    for cap in records:
        cid = cap.get("capsule_id", "?")[:col_id]
        action_id = cap.get("action_id", "?")
        action = action_id.split("/")[0] if "/" in action_id else action_id
        action = action[:col_action]
        operator = cap.get("operator", "")[:col_op]

        eff = cap.get("effect", {}) or {}
        eff_str = ""
        if eff:
            eff_str = f"{eff.get('type', '')}:{eff.get('status', '')}"
        eff_str = eff_str[:col_effect]

        disp = cap.get("disposition", {}) or {}
        verdict = disp.get("verdict_class", "")[:col_verdict]

        chain = cap.get("chain", {}) or {}
        chain_str = ""
        if chain:
            parent = chain.get("parent_capsule_id", "")
            rel = chain.get("relation", "")
            chain_str = f"{rel}→{parent[:8]}…" if parent else ""

        print(
            f"{cid:<{col_id}}  "
            f"{action:<{col_action}}  "
            f"{operator:<{col_op}}  "
            f"{eff_str:<{col_effect}}  "
            f"{verdict:<{col_verdict}}  "
            f"{chain_str}",
            file=out,
        )
    print(file=out)


# ---------------------------------------------------------------------------
# L2 — chain tree
# ---------------------------------------------------------------------------

def view_chains(path: str | os.PathLike, *, out: Any = None) -> None:
    """L2: chain-tree view — groups capsules by their chain parent.

    Roots (capsules with no parent) are printed first; each confirmed/chained
    child is indented under its parent.  Orphaned children (parent not in
    ledger) appear at the end under an ``[orphaned]`` header.

    Args:
        path: Path to the JSONL ledger file.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    if not records:
        print(f"ledger: {path} — empty or not found", file=out)
        return

    by_id: dict[str, dict] = {c["capsule_id"]: c for c in records if "capsule_id" in c}
    children: dict[str, list[str]] = {}
    for cap in records:
        chain = cap.get("chain") or {}
        parent = chain.get("parent_capsule_id")
        if parent:
            children.setdefault(parent, []).append(cap["capsule_id"])

    printed: set[str] = set()

    def _action(cap: dict) -> str:
        aid = cap.get("action_id", "?")
        return aid.split("/")[0] if "/" in aid else aid

    def _verdict(cap: dict) -> str:
        return (cap.get("disposition") or {}).get("verdict_class", "")

    def _model(cap: dict) -> str:
        ma = cap.get("model_attestation") or {}
        mid = ma.get("model_id") or ""
        prov = ma.get("provider") or ""
        if mid:
            return f"{prov}/{mid}" if prov else mid
        return ""

    def _print_node(cid: str, depth: int) -> None:
        if cid in printed:
            return
        printed.add(cid)
        cap = by_id.get(cid, {})
        indent = "  " * depth
        connector = "└─ " if depth else ""
        action = _action(cap)
        verdict = _verdict(cap)
        model_str = _model(cap)
        short_id = cid[:12]
        chain = cap.get("chain") or {}
        rel = chain.get("relation", "")
        rel_tag = f"[{rel}] " if rel and depth else ""
        model_tag = f"  model={model_str}" if model_str else ""
        print(
            f"{indent}{connector}{short_id}…  {action}  {rel_tag}{verdict}{model_tag}",
            file=out,
        )
        for child_id in children.get(cid, []):
            _print_node(child_id, depth + 1)

    print(f"\ncapsule-emit ledger (chains): {path}  ({len(records)} record(s))\n", file=out)

    roots = [c["capsule_id"] for c in records if not (c.get("chain") or {}).get("parent_capsule_id") and "capsule_id" in c]
    for root_id in roots:
        _print_node(root_id, 0)

    orphans = [cid for cid in by_id if cid not in printed]
    if orphans:
        print("\n[orphaned — parent not in ledger]", file=out)
        for cid in orphans:
            _print_node(cid, 1)

    print(file=out)


# ---------------------------------------------------------------------------
# L3 — full single-capsule detail
# ---------------------------------------------------------------------------

def show(
    path: str | os.PathLike,
    capsule_id: str,
    *,
    out: Any = None,
) -> bool:
    """L3: two-tier detail view for a single capsule.

    Prints the top-level fields first, then the nested attestation and chain
    blocks.  Returns ``True`` when found, ``False`` when not.

    Args:
        path: Path to the JSONL ledger file.
        capsule_id: Full or prefix (≥8 chars) capsule_id to look up.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    cap = None
    for rec in records:
        rid = rec.get("capsule_id", "")
        if rid == capsule_id or rid.startswith(capsule_id):
            cap = rec
            break

    if cap is None:
        print(f"capsule {capsule_id!r} not found in {path}", file=out)
        return False

    cid = cap.get("capsule_id", "?")
    seq = next(
        (i for i, r in enumerate(read_ledger_entries(path), start=1) if r.get("capsule_id") == cid),
        None,
    )
    leaf_str = f"  #logged @ leaf {seq}" if seq is not None else ""
    print(f"\n── capsule {cid} ──{leaf_str}\n", file=out)

    # Tier 1: top-level identity fields
    _field(out, "format_version", cap.get("format_version"))
    _field(out, "operator", cap.get("operator"))
    _field(out, "developer", cap.get("developer"))
    action_id = cap.get("action_id", "")
    action_name = action_id.split("/")[0] if "/" in action_id else action_id
    _field(out, "action", action_name)
    _field(out, "action_id", action_id)
    _field(out, "ts", cap.get("ts"))

    # Tier 2: nested blocks
    _block(out, "disposition", cap.get("disposition"))
    _block(out, "effect", cap.get("effect"))
    _block(out, "chain", cap.get("chain"))

    ma = cap.get("model_attestation") or {}
    _field(out, "model_attestation.model_id", ma.get("model_id"))
    _field(out, "model_attestation.provider", ma.get("provider"))
    ca = ma.get("compute_attestation") or {}
    if ca:
        _block(out, "compute_attestation", ca)

    assurance = cap.get("assurance") or {}
    if assurance:
        _block(out, "assurance", assurance)

    print(file=out)
    return True


def _field(out: Any, label: str, value: Any) -> None:
    if value is None or value == "" or value == {}:
        return
    print(f"  {label:<32} {value}", file=out)


def _block(out: Any, label: str, block: Any) -> None:
    if not block:
        return
    print(f"  {label}:", file=out)
    if isinstance(block, dict):
        for k, v in block.items():
            if v is not None and v != "":
                print(f"    {k:<30} {v}", file=out)
    else:
        print(f"    {block}", file=out)
