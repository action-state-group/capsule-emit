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
accumulated since the last checkpoint for that ledger -- **or**
``cadence_seconds`` has elapsed since the first unwitnessed entry after the
last checkpoint, whichever comes first -- the actual checkpoint build (an MMR
sync + peaks + signature -- all local, no network) and its TS registration
(the only network call, and the only thing that leaves the process) are
dispatched on a daemon thread -- async, fire-and-forget, exactly like the
already-default per-emit anchor. The counter (and the age clock) reset
synchronously, before the thread is dispatched, specifically so a burst of
``emit()`` calls crossing the boundary while the worker is still running never
pack the queue with redundant checkpoint attempts.

**The age leg only ever fires when there is unwitnessed work.** There is no
background timer or polling thread -- age is checked lazily, only inside
``maybe_checkpoint``, which itself only ever runs right after a real
``emit()`` appended a new entry. An idle log (no ``emit()`` calls) never
calls this function at all, so it is silent by construction: it is
structurally impossible for the age leg to fire a checkpoint with zero
unwitnessed entries. The checkpoint-stamp entries this module persists (see
``_persist_checkpoint_stamp``) reinforce this rather than undermine it: they
are written directly through ``ledger.append_to_ledger``, never through
``core.emit()``/``maybe_checkpoint``, so persisting a stamp never advances the
counter *or* resets the age clock -- exactly like it never advances the
entry-count cadence today.

**Digest-only.** The only bytes that ever cross the wire are the checkpoint's
own SHA-256 digest (``CheckpointRecord.digest()``, itself a hash of hashes --
see ``capsule_emit.checkpoint.emit``). No capsule content, no capsule_id list,
no ledger path, ever leaves the process -- same posture as the anchor.

**Signing (checkpoint layer only -- this is NOT capsule content signing).**
No extra key management is required to get the default path working: since
0.5.0's checkpoint-signer precondition, a checkpoint is signed by the SAME
persisted Ed25519 identity that signs capsule content -- resolved via
``capsule_emit.signing.resolve_signer`` with the identical precedence
``seal()`` uses (an explicit ``signer=`` object, else ``signing_key_path=``,
else ``CAPSULE_SIGNING_KEY_PATH``, else a key file next to the ledger). A
checkpoint signed in one process therefore verifies in a later one -- the
persisted key survives a restart, unlike this module's old default,
:class:`_AutoSigner` (ephemeral, in-process HMAC-SHA256, regenerated every
process -- retired from the default path because a checkpoint signed with it
could never be verified again once that process exited). ``_AutoSigner``
stays defined for direct, explicit use (e.g. a test double that wants a
signer with zero filesystem footprint); nothing in the default ``emit()``
path reaches for it anymore. **The checkpoint signer only ever signs MMR
checkpoint digests, never capsule content** -- see
:class:`_PersistedCheckpointSigner`, the thin adapter that lets a
``capsule_emit.signing.Signer`` (atomic ``sign(bytes) -> (signature,
key_id)``) satisfy the checkpoint layer's own ``Signer`` protocol (a static
``key_id`` attribute plus ``sign(digest_hex) -> str``). The two call sites
remain independent modules for the same reason as before: this one still
works with nothing configured even though capsule-level signing now always
runs -- they merely resolve to the same key by default now, instead of two
unrelated ones.

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

from . import signing as _signing

__all__ = [
    "maybe_checkpoint",
    "witness_enabled",
    "WITNESS_ENV_VAR",
    "WITNESS_URL_ENV_VAR",
    "CADENCE_ENV_VAR",
    "DEFAULT_CADENCE_ENTRIES",
    "AGE_CADENCE_ENV_VAR",
    "DEFAULT_CADENCE_SECONDS",
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

#: How many seconds may elapse since the first unwitnessed entry after the
#: last checkpoint before one comes due on age alone -- the other leg of
#: "100 entries or 15 minutes, whichever first" (frozen surface §0). Only
#: ever consulted when at least one unwitnessed entry exists (see the module
#: docstring's "due" section) -- an idle log never trips this.
AGE_CADENCE_ENV_VAR = "CAPSULE_WITNESS_CADENCE_SECONDS"
DEFAULT_CADENCE_SECONDS = 900

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


def _resolved_age_cadence(override: float | None) -> float:
    if override is not None:
        return override
    raw = os.environ.get(AGE_CADENCE_ENV_VAR)
    if raw is None:
        return DEFAULT_CADENCE_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_CADENCE_SECONDS
    return parsed if parsed > 0 else DEFAULT_CADENCE_SECONDS


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
    """Ephemeral, in-process HMAC-SHA256 checkpoint signer -- RETIRED from the
    default checkpoint path (see the module docstring's "Signing" section):
    its secret is never persisted, so a checkpoint it signs cannot be
    verified once the process that produced it exits. Kept only as an
    explicit opt-in test double for a caller that wants a signer with zero
    filesystem footprint."""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id
        self._secret = secrets.token_bytes(32)

    def sign(self, digest_hex: str) -> str:
        return hmac.new(self._secret, digest_hex.encode("ascii"), hashlib.sha256).hexdigest()


class _PersistedCheckpointSigner:
    """Adapts a ``capsule_emit.signing.Signer`` (frozen §7d: atomic
    ``sign(bytes) -> (signature, key_id)``) to the checkpoint layer's own
    ``Signer`` protocol (a static ``key_id`` attribute plus
    ``sign(digest_hex) -> str``, see ``capsule_emit.checkpoint.emit.Signer``)
    -- this is what lets the default checkpoint path sign with the SAME
    persisted Ed25519 identity ``seal()`` uses for capsule content, instead
    of :class:`_AutoSigner`'s ephemeral per-process key."""

    def __init__(self, signing_signer: _signing.Signer) -> None:
        self._signing_signer = signing_signer
        self.key_id = signing_signer.key_id

    def sign(self, digest_hex: str) -> str:
        signature_hex, _key_id = self._signing_signer.sign(digest_hex.encode("ascii"))
        return signature_hex


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
        from .ledger import read_ledger_entries

        # The raw, unfiltered file -- checkpoint-stamp records included -- so
        # a stamp this module persists (see ``_build_and_register``) is
        # itself indexed as an MMR leaf and ends up covered by the *next*
        # checkpoint, same as any capsule.
        for i, record in enumerate(read_ledger_entries(self._path), start=1):
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

#: ``time.monotonic()`` timestamp of the first unwitnessed entry since the
#: last checkpoint for this ledger path -- the age leg's clock. Guarded by
#: ``_count_lock`` (same lifecycle as ``_counts``: set the moment a fresh
#: window opens, reset at dispatch, left alone when dispatch is deferred to
#: an in-flight worker).
_armed_at: dict[str, float] = {}

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


def _get_state(ledger_path: str, signer: _signing.Signer | None = None) -> _WitnessState:
    """Only ever called once a checkpoint is actually due -- this is the one
    place that imports ``capsule_emit.checkpoint``.

    ``signer`` is the already-resolved ``capsule_emit.signing.Signer``
    ``core.emit()`` just signed this capsule with (same key resolution as
    ``seal()``: ``signer=``/``signing_key_path=``/env/per-ledger default) --
    passed through so the checkpoint layer signs with the identical
    persisted identity instead of resolving (and potentially caching) a
    second one. When omitted (a caller driving ``maybe_checkpoint`` directly)
    it is resolved here, against this same ``ledger_path``, with that same
    precedence."""
    from .checkpoint import MmrLedger

    key = _resolve_key(ledger_path)
    with _state_lock:
        state = _states.get(key)
        if state is None:
            resolved_signer = signer if signer is not None else _signing.resolve_signer(ledger_path)
            state = _WitnessState(
                mmr=MmrLedger(_JsonlLogSource(ledger_path)),
                signer=_PersistedCheckpointSigner(resolved_signer),
                log_id=key,
            )
            _states[key] = state
        return state


def _persist_checkpoint_stamp(cp: Any, ledger_path: str) -> None:
    """Write ``cp`` (with whatever ``WitnessRecord`` s it collected) back into
    its own ledger as a checkpoint-stamp entry -- see ``ledger.py``'s module
    docstring for the shape and why. Never called before ``cp.witnesses`` is
    final for this round: once written, the entry is immediately eligible to
    be folded into the MMR by the next ``state.mmr.sync()``, so writing it
    mid-registration could let a later witness append race an already-synced
    read of ``cp.witnesses`` elsewhere.

    Best-effort: a failure to persist the stamp must not raise into
    ``emit()`` (this already runs on the fire-and-forget witness thread) --
    it only means this checkpoint's evidence-of-witnessing isn't itself
    logged; the checkpoint and any COSE receipts already obtained are
    unaffected.
    """
    from .ledger import CHECKPOINT_STAMP_KIND, append_to_ledger

    entry = {
        "kind": CHECKPOINT_STAMP_KIND,
        "v": 1,
        # entry_digest(), not digest(): the leaf this entry becomes must
        # commit to the FULL persisted checkpoint -- signature and
        # witnesses included -- not just the signing body. See
        # CheckpointRecord.entry_digest()'s docstring.
        "capsule_id": cp.entry_digest(),
        "checkpoint": cp.to_dict(),
    }
    try:
        append_to_ledger(entry, ledger_path)
    except OSError as exc:  # noqa: BLE001 -- fire-and-forget, never raises into emit()
        warnings.warn(
            f"capsule-emit: failed to persist checkpoint stamp for log_id="
            f"{cp.log_id!r} mmr_size={cp.mmr_size} to {ledger_path!r}: {exc}",
            RuntimeWarning,
            stacklevel=1,
        )


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

    # Persist the finished checkpoint (witnessed or, if every endpoint
    # failed above, still self-attested) as its own log entry -- it becomes
    # a leaf ``state.mmr.sync()`` folds in on the *next* due cycle, so the
    # next checkpoint's root genuinely covers this one's stamp. Written
    # regardless of registration outcome: even a self-attested checkpoint is
    # history worth logging, and item 5's idle-silence/stamp-exclusion rule
    # (audit item 5) depends on stamp entries existing in the log at all.
    _persist_checkpoint_stamp(cp, state.log_id)


def maybe_checkpoint(
    ledger_path: str,
    *,
    ts_url: str | list[str] | None = None,
    enabled: bool | None = None,
    cadence_entries: int | None = None,
    cadence_seconds: float | None = None,
    signer: _signing.Signer | None = None,
) -> None:
    """Call once per ``emit()``, after the capsule is appended to the ledger.

    Cheap in the common (non-streaming) case: one dict lookup and one int
    increment under a lock, no import of ``capsule_emit.checkpoint``, no
    network call -- until ``cadence_entries`` calls have accumulated since the
    last checkpoint for this exact ``ledger_path``, **or** ``cadence_seconds``
    have elapsed since the first unwitnessed entry after the last checkpoint
    -- whichever comes first. At that point the checkpoint build + TS
    registration is dispatched on a daemon thread and this function returns
    immediately either way; it never blocks ``emit()``. The age leg is only
    ever evaluated here, on the back of a real new entry -- there is no
    background timer, so a ledger with no new ``emit()`` calls is never
    checkpointed on age alone (see the module docstring's "due" section).

    ``ts_url`` accepts a single endpoint or several (a list, or a
    comma-separated string) -- the due checkpoint is registered with every
    endpoint named, independently (see :func:`_parse_witness_urls`).

    ``signer`` is the ``capsule_emit.signing.Signer`` already resolved for
    this ``ledger_path`` (``core.emit()`` passes the same object it just
    signed the capsule with); omitted, it is resolved here against
    ``ledger_path`` with the same precedence -- see :func:`_get_state`. Only
    consulted the first time a given ``ledger_path`` actually reaches its
    checkpoint-due state (the resolved signer is then cached on that
    ledger's :class:`_WitnessState` for every checkpoint after).

    Prints the one-time first-use notice (see :func:`_print_first_use_notice_once`)
    on the first call where witnessing is enabled -- before the cadence check,
    so it fires at the first ``seal()``, not the first checkpoint actually due.
    """
    if not witness_enabled(enabled):
        return

    urls = _parse_witness_urls(ts_url)
    _print_first_use_notice_once(urls)

    cadence = _resolved_cadence(cadence_entries)
    age_cadence = _resolved_age_cadence(cadence_seconds)
    key = _resolve_key(ledger_path)
    now = time.monotonic()
    with _count_lock:
        count = _counts.get(key, 0) + 1
        armed_at = _armed_at.setdefault(key, now)
        age = now - armed_at
        if count < cadence and age < age_cadence:
            _counts[key] = count
            return

    # Due (entry-count or age leg). Claim the per-ledger dispatch slot -- if
    # a worker for this exact ledger is already in flight, leave the count
    # (and the age clock) where they are -- still due -- and defer to that
    # worker; it will observe everything currently on disk when it runs its
    # own mmr.sync(), so nothing is lost, and the next call to cross this
    # check (once the in-flight worker releases the slot) will dispatch the
    # catch-up checkpoint.
    dispatch_lock = _dispatch_lock_for(key)
    if not dispatch_lock.acquire(blocking=False):
        with _count_lock:
            _counts[key] = count
        return

    # Reset only once dispatch is actually claimed -- both the counter and
    # the age clock, so the next unwitnessed entry opens a fresh window.
    with _count_lock:
        _counts[key] = 0
        _armed_at[key] = time.monotonic()

    # Only now -- once a checkpoint is actually due -- does this touch the
    # MMR-bearing state, which is what imports capsule_emit.checkpoint.
    state = _get_state(ledger_path, signer)

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
