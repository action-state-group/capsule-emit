# SPDX-License-Identifier: Apache-2.0
"""Default-on CLL checkpoint/witness wiring for ``capsule_emit.core.emit()``.

Since 0.5.0 the checkpoint/witness differentiator (``capsule_emit.checkpoint``)
is wired into the default ``emit()`` path instead of requiring a caller to
``import capsule_emit.checkpoint`` by hand. This module is that wiring, and it
is deliberately split from ``capsule_emit.checkpoint`` itself so the split
stays honest:

- **This module** is imported unconditionally by ``capsule_emit.core`` (it is
  stdlib-only at module scope -- no MMR, no network, no ``checkpoint``
  subpackage import). Its job is the *lazy, per-ledger* decision of whether a
  checkpoint is due yet.
- **``capsule_emit.checkpoint``** (the MMR/CLL core) is imported from inside
  this module's functions ONLY once that decision comes back "yes" -- so a
  caller who emits once and exits, or whose ledger never crosses the cadence
  threshold, never pays for an MMR that never does anything. This is what
  keeps ``tests/test_checkpoint_layer0_cost.py`` green while the default is
  now ON: that test asserts ``capsule_emit.checkpoint`` is absent from
  ``sys.modules`` after import-time only, and import-time is exactly what
  this split protects.

**What "due" means.** Every ``emit()`` call increments an in-process,
per-ledger-path counter (an int; no I/O). Once ``cadence_entries`` calls have
accumulated since the last checkpoint for that ledger, the actual checkpoint
build (an MMR sync + peaks + signature -- all local, no network) and its TS
registration (the only network call, and the only thing that leaves the
process) are dispatched on a daemon thread -- async, fire-and-forget, exactly
like the already-default per-emit anchor. The counter resets synchronously,
before the thread is dispatched, specifically so a burst of ``emit()`` calls
crossing the boundary while the worker is still running never pack the queue
with redundant checkpoint attempts.

**Digest-only.** The only bytes that ever cross the wire are the checkpoint's
own SHA-256 digest (``CheckpointRecord.digest()``, itself a hash of hashes --
see ``capsule_emit.checkpoint.emit``). No capsule content, no capsule_id list,
no ledger path, ever leaves the process -- same posture as the anchor.

**Signing.** No external key management is required to get the default path
working: an ephemeral HMAC-SHA256 key is generated once per ledger path,
in-process (:class:`_AutoSigner`). It is good enough for what the default
path's own checkpoint chain needs (rollback/consistency self-detection across
the *lifetime of one process*) but is not persisted, so it does not survive a
process restart and is not suitable for a deployment that wants a stable,
externally-attributable signing identity -- that caller should use
``capsule_emit.checkpoint``'s primitives directly with their own
:class:`~capsule_emit.checkpoint.Signer`.

**Off switch.** ``emit(..., witness=False)`` or the ``CAPSULE_WITNESS=off``
env var (checked only when the ``witness`` kwarg is left at its default,
``None`` -- an explicit ``True``/``False`` always wins).

**Multiple witnesses.** ``witness_url=`` (and ``CAPSULE_WITNESS_URL``) accept
either a single endpoint or several -- a list, or a comma-separated string
(the env var is string-only, so that's its multi-value shape). Every checkpoint
that comes due is registered with *each* endpoint independently; one endpoint
failing never blocks the others (see ``_build_and_register``). Registering
with more than one independently-operated Transparency Service is what climbs
from the *witnessed (single witness)* tier to the *multi-witness,
equivocation-resistant* tier -- see ``docs/checkpoint.md``.

**First-use notice.** Printed once per process, to stderr, at the first
``maybe_checkpoint()`` call where witnessing is enabled -- before the first
byte ever leaves the process, independent of whether a checkpoint is
actually due yet (the cadence counter may not cross its threshold for a
long time, or ever, in a short-lived process). States what will be sent (a
32-byte digest -- structurally incapable of carrying capsule content), where
(the resolved endpoint(s)), and how to turn it off. Printed exactly once per
process regardless of how many ledgers or checkpoints follow (see
``_print_first_use_notice_once``).
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "maybe_checkpoint",
    "witness_enabled",
    "WITNESS_ENV_VAR",
    "WITNESS_URL_ENV_VAR",
    "CADENCE_ENV_VAR",
    "DEFAULT_CADENCE_ENTRIES",
]

#: Explicit ``witness=`` always wins; this env var is consulted only when the
#: caller leaves ``witness`` at its default (``None``). Any of these values
#: (case-insensitive) turns witnessing off; anything else -- including unset
#: -- leaves it on, matching the anchor's already-default-ON posture.
WITNESS_ENV_VAR = "CAPSULE_WITNESS"
_OFF_VALUES = {"off", "0", "false", "no"}

#: Overrides the default TS URL for the witness path specifically (mirrors
#: ``AAC_ANCHOR_URL`` for the anchor path). ``emit(..., witness_url=...)``
#: takes precedence over this. Accepts one URL, or several as a
#: comma-separated string -- each due checkpoint is registered with every
#: endpoint named. See :func:`_parse_witness_urls`.
WITNESS_URL_ENV_VAR = "CAPSULE_WITNESS_URL"

#: How many ``emit()`` calls (for the same ledger path) accumulate before a
#: checkpoint is built and registered. Matches
#: ``capsule_emit.checkpoint.CheckpointConfig``'s own default so there is one
#: default cadence, not two numbers to reconcile in docs.
CADENCE_ENV_VAR = "CAPSULE_WITNESS_CADENCE_ENTRIES"
DEFAULT_CADENCE_ENTRIES = 100

#: How long the atexit handler waits, in total, for outstanding witness
#: threads before giving up and warning. Overridable for tests.
_ATEXIT_WITNESS_TIMEOUT = float(os.environ.get("CAPSULE_EMIT_ATEXIT_WITNESS_TIMEOUT", "5.0"))


def witness_enabled(explicit: bool | None) -> bool:
    """Resolve the on/off decision: ``explicit`` (the ``witness=`` kwarg) wins
    when set; otherwise ``CAPSULE_WITNESS`` is consulted, defaulting to on."""
    if explicit is not None:
        return explicit
    return os.environ.get(WITNESS_ENV_VAR, "").strip().lower() not in _OFF_VALUES


def _resolved_cadence(override: int | None) -> int:
    if override is not None:
        return override
    raw = os.environ.get(CADENCE_ENV_VAR)
    if raw is None:
        return DEFAULT_CADENCE_ENTRIES
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_CADENCE_ENTRIES
    return parsed if parsed > 0 else DEFAULT_CADENCE_ENTRIES


def _parse_witness_urls(raw: str | list[str] | None) -> list[str]:
    """Normalize a ``witness_url=`` / ``CAPSULE_WITNESS_URL`` value into a
    list of endpoints, in the order given, with blanks dropped and duplicates
    removed. Accepts a single URL string, a list of URL strings, or a
    comma-separated string (the shape an env var must take). An empty result
    means "no override" -- the caller falls back to the registered default.
    """
    if raw is None:
        return []
    candidates = raw.split(",") if isinstance(raw, str) else list(raw)
    seen: set[str] = set()
    urls: list[str] = []
    for candidate in candidates:
        url = candidate.strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


_notice_lock = threading.Lock()
_notice_printed = False


def _print_first_use_notice_once(urls: list[str]) -> None:
    """Print the one-time, first-use witness notice to stderr.

    Fires exactly once per process, at the first ``maybe_checkpoint()`` call
    where witnessing is enabled -- i.e. at the first ``seal()``, before any
    checkpoint has actually gone out over the network, not gated on the
    cadence counter reaching its threshold. Never raises; a broken stderr
    must not break emit()."""
    global _notice_printed
    with _notice_lock:
        if _notice_printed:
            return
        _notice_printed = True
    try:
        endpoints = ", ".join(urls) if urls else "the default witness endpoint"
        print(
            "capsule-emit: witnessing is on for this process -- once a checkpoint "
            "is due, a signed ~32-byte digest (sha256 of the checkpoint, structurally "
            f"incapable of carrying your capsule content) will be sent to {endpoints}. "
            "Disable with emit(..., witness=False) or CAPSULE_WITNESS=off. "
            "(This notice prints once per process, before any checkpoint goes out.)",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 -- a notice must never break emit()
        pass


class _AutoSigner:
    """Zero-config default checkpoint signer -- see the module docstring's
    "Signing" section for what this does and does not provide."""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id
        self._secret = secrets.token_bytes(32)

    def sign(self, digest_hex: str) -> str:
        return hmac.new(self._secret, digest_hex.encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Rec:
    seq: int
    capsule_id: str


class _JsonlLogSource:
    """Read-only adapter: capsule-emit's JSONL ledger file, shaped to the
    checkpoint subpackage's structural ``LogSource`` (see
    ``capsule_emit.checkpoint.index``). Writes stay owned exclusively by
    ``capsule_emit.ledger.append_to_ledger`` -- this source never writes."""

    def __init__(self, path: str) -> None:
        self._path = path

    def scan(self, query: Any = None):
        from .ledger import read_ledger

        for i, record in enumerate(read_ledger(self._path), start=1):
            yield _Rec(seq=i, capsule_id=record["capsule_id"])

    def append(self, capsule: dict, *, consequential: bool = True) -> Any:
        raise NotImplementedError(
            "read-only: capsule_emit.ledger.append_to_ledger owns writes to this file"
        )

    def fetch(self, capsule_id: str) -> _Rec | None:
        for rec in self.scan():
            if rec.capsule_id == capsule_id:
                return rec
        return None

    def verify(self, capsule_id: str) -> _Rec | None:
        return self.fetch(capsule_id)

    def find_gaps(self) -> list:
        return []


@dataclass
class _WitnessState:
    mmr: Any
    signer: Any
    log_id: str
    prev: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


#: Cheap per-ledger-path entry counters -- plain ints, no MMR, no checkpoint
#: import. This is what every below-cadence ``emit()`` call touches, and it
#: is what keeps that path free of any ``capsule_emit.checkpoint`` import.
_count_lock = threading.Lock()
_counts: dict[str, int] = {}

#: The heavier, MMR-bearing state -- created lazily, only once a ledger path
#: actually crosses the cadence threshold for the first time.
_state_lock = threading.Lock()
_states: dict[str, _WitnessState] = {}

_pending_lock = threading.Lock()
_pending: dict[str, threading.Thread] = {}

#: One dispatch in flight per ledger path at a time. A non-blocking
#: ``acquire`` is the dispatch gate itself: if it fails, a worker for this
#: ledger is already running and will pick up everything currently on disk
#: when it gets to its own ``mmr.sync()`` -- dispatching a second, overlapping
#: worker would only race the first one to the same MMR state and produce a
#: spurious "nothing new to checkpoint" ``RollbackError`` (observed under a
#: tight loop with a low cadence during development of this module).
_dispatch_locks_guard = threading.Lock()
_dispatch_locks: dict[str, threading.Lock] = {}


def _dispatch_lock_for(key: str) -> threading.Lock:
    with _dispatch_locks_guard:
        lock = _dispatch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _dispatch_locks[key] = lock
        return lock


def _resolve_key(ledger_path: str) -> str:
    return str(Path(ledger_path).resolve())


def _get_state(ledger_path: str) -> _WitnessState:
    """Only ever called once a checkpoint is actually due -- this is the one
    place that imports ``capsule_emit.checkpoint``."""
    from .checkpoint import MmrLedger

    key = _resolve_key(ledger_path)
    with _state_lock:
        state = _states.get(key)
        if state is None:
            state = _WitnessState(
                mmr=MmrLedger(_JsonlLogSource(ledger_path)),
                signer=_AutoSigner(f"capsule-emit-auto/{secrets.token_hex(4)}"),
                log_id=key,
            )
            _states[key] = state
        return state


def _build_and_register(state: _WitnessState, ts_urls: list[str]) -> None:
    from .checkpoint import (
        DEFAULT_TS_URL,
        CheckpointError,
        RollbackError,
        emit_checkpoint,
        register_checkpoint,
    )

    resolved_urls = ts_urls or [DEFAULT_TS_URL]

    with state.lock:
        state.mmr.sync()
        try:
            cp = emit_checkpoint(state.mmr, state.signer, log_id=state.log_id, prev=state.prev)
        except (CheckpointError, RollbackError) as exc:
            warnings.warn(
                f"capsule-emit: witness checkpoint build for log_id={state.log_id!r} "
                f"failed: {exc}",
                RuntimeWarning,
                stacklevel=1,
            )
            return
        state.prev = cp

    # Fan the same checkpoint out to every endpoint independently -- one
    # endpoint failing must never block registration with the others.
    for url in resolved_urls:
        try:
            witness_record = register_checkpoint(cp, url)
            cp.witnesses.append(witness_record)
        except Exception as exc:  # noqa: BLE001 -- fire-and-forget, never raises into emit()
            warnings.warn(
                f"capsule-emit: witness registration for checkpoint log_id={state.log_id!r} "
                f"mmr_size={cp.mmr_size} to {url} did not complete: {exc}",
                RuntimeWarning,
                stacklevel=1,
            )


def maybe_checkpoint(
    ledger_path: str,
    *,
    ts_url: str | list[str] | None = None,
    enabled: bool | None = None,
    cadence_entries: int | None = None,
) -> None:
    """Call once per ``emit()``, after the capsule is appended to the ledger.

    Cheap in the common (non-streaming) case: one dict lookup and one int
    increment under a lock, no import of ``capsule_emit.checkpoint``, no
    network call -- until ``cadence_entries`` calls have accumulated since the
    last checkpoint for this exact ``ledger_path``. At that point the
    checkpoint build + TS registration is dispatched on a daemon thread and
    this function returns immediately either way; it never blocks ``emit()``.

    ``ts_url`` accepts a single endpoint or several (a list, or a
    comma-separated string) -- the due checkpoint is registered with every
    endpoint named, independently (see :func:`_parse_witness_urls`).

    Prints the one-time first-use notice (see :func:`_print_first_use_notice_once`)
    on the first call where witnessing is enabled -- before the cadence check,
    so it fires at the first ``seal()``, not the first checkpoint actually due.
    """
    if not witness_enabled(enabled):
        return

    urls = _parse_witness_urls(ts_url)
    _print_first_use_notice_once(urls)

    cadence = _resolved_cadence(cadence_entries)
    key = _resolve_key(ledger_path)
    with _count_lock:
        count = _counts.get(key, 0) + 1
        if count < cadence:
            _counts[key] = count
            return

    # Due. Claim the per-ledger dispatch slot -- if a worker for this exact
    # ledger is already in flight, leave the count where it is (still >=
    # cadence) and defer to that worker; it will observe everything currently
    # on disk when it runs its own mmr.sync(), so nothing is lost, and the
    # next call to cross this check (once the in-flight worker releases the
    # slot) will dispatch the catch-up checkpoint.
    dispatch_lock = _dispatch_lock_for(key)
    if not dispatch_lock.acquire(blocking=False):
        with _count_lock:
            _counts[key] = count
        return

    # Reset only once dispatch is actually claimed.
    with _count_lock:
        _counts[key] = 0

    # Only now -- once a checkpoint is actually due -- does this touch the
    # MMR-bearing state, which is what imports capsule_emit.checkpoint.
    state = _get_state(ledger_path)

    def _worker() -> None:
        try:
            _build_and_register(state, urls)
        finally:
            with _pending_lock:
                _pending.pop(state.log_id, None)
            dispatch_lock.release()

    thread = threading.Thread(target=_worker, daemon=True, name="capsule-emit-witness")
    with _pending_lock:
        _pending[state.log_id] = thread
    thread.start()


def _join_pending_at_exit() -> None:
    with _pending_lock:
        pending = list(_pending.items())
    if not pending:
        return
    deadline = time.monotonic() + _ATEXIT_WITNESS_TIMEOUT
    for log_id, thread in pending:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
        if thread.is_alive():
            warnings.warn(
                f"capsule-emit: witness checkpoint for log_id={log_id!r} did not "
                f"complete before interpreter shutdown (joined for "
                f"{_ATEXIT_WITNESS_TIMEOUT}s) -- outcome unknown, the checkpoint "
                "may not have been registered.",
                RuntimeWarning,
                stacklevel=1,
            )


atexit.register(_join_pending_at_exit)
