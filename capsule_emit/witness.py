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

**Witness outage: durable, per-witness retry -- not a drop.** A checkpoint
that fails to register with a witness is still persisted (as a
self-attested ``CHECKPOINT_STAMP_KIND`` entry, above) -- so it already
survives a restart, but nothing retried it. This module closes that gap
with a durable backlog that is the ledger itself, not a separate in-memory
or side-file queue:

- Every checkpoint stamp already on disk that a given witness URL has not
  yet confirmed (checked against that stamp's own ``witnesses`` list, plus
  any later backfill -- see below) is, by definition, that witness's
  pending backlog. :func:`checkpoint_witness_backlog` computes it fresh
  from the ledger on every call -- same precedent as ``MmrLedger.sync()``'s
  full rescan (O16-18: "no persisted cursor spanning an off period ...
  this already holds structurally"). There is nothing to lose on restart
  because there is nothing kept only in memory: the pending set is a pure
  function of what is already durably on disk, so it cannot desync from a
  mutable cursor file, and it cannot grow the process's own memory
  footprint -- it grows (bounded, one entry per outage-era checkpoint) only
  in the same durable ledger every stamp already lives in.
- :func:`retry_pending_witness_stamps` drains each configured witness's
  backlog independently -- **a per-witness cursor**, oldest pending
  checkpoint first, stopping at that witness's first failure this call (it
  is presumably still down; the next call resumes from the same point,
  since nothing is marked done until a backfill entry is durably
  persisted). One witness being down never blocks another's drain -- each
  URL's loop is independent, mirroring the existing any-one-endpoint-down
  fan-out isolation in :func:`_build_and_register`.
- A late success cannot rewrite the original (already-leaved) checkpoint
  stamp, so it is persisted as its own small ``WITNESS_BACKFILL_KIND``
  entry citing the checkpoint's ``entry_digest`` -- see
  :func:`_persist_witness_backfill`.
- ``_build_and_register`` calls :func:`retry_pending_witness_stamps` before
  handling the checkpoint newly due this cycle, so the backlog is what
  drains on the very next real ``emit()``/``seal()`` after a witness
  returns -- consistent with this module's existing "no background timer,
  driven only by real writes" design (see the "due" section above). An
  operator who wants to force a drain without waiting on the next emit can
  call :func:`retry_pending_witness_stamps` directly.
- ``capsule_emit.status.compute_status`` folds backfills into its grade and
  lag numbers, so a checkpoint that was self-attested during an outage and
  later backfilled is honestly reported as witnessed once the backfill
  lands -- never stuck showing "awaiting stamp" forever, and never shown as
  witnessed before a stamp genuinely arrives.
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import json
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
    "witness_mode",
    "witness_is_stub",
    "refuse_stub_in_production",
    "StubWitnessInProductionError",
    "WITNESS_ENV_VAR",
    "WITNESS_URL_ENV_VAR",
    "CADENCE_ENV_VAR",
    "DEFAULT_CADENCE_ENTRIES",
    "AGE_CADENCE_ENV_VAR",
    "DEFAULT_CADENCE_SECONDS",
    "CAPSULE_ENV_VAR",
    "resolved_witness_urls",
    "CheckpointWitnessState",
    "checkpoint_witness_states",
    "checkpoint_witness_backlog",
    "retry_pending_witness_stamps",
]

#: Explicit ``witness=`` always wins; this env var is consulted only when the
#: caller leaves ``witness`` at its default (``None``). Any of these values
#: (case-insensitive) turns witnessing off; ``"stub"`` arms the in-process
#: stub witness (see :func:`witness_mode`); anything else -- including unset
#: -- leaves it on, matching the anchor's already-default-ON posture.
WITNESS_ENV_VAR = "CAPSULE_WITNESS"
_OFF_VALUES = {"off", "0", "false", "no"}
_STUB_VALUES = {"stub"}

#: Deployment-posture env var (frozen surface §1a.4). Only ever consulted to
#: refuse ``CAPSULE_WITNESS=stub`` at startup -- it names no other behavior
#: here. Case-insensitive; only the literal value below is production.
CAPSULE_ENV_VAR = "CAPSULE_ENV"
_PRODUCTION_ENV_VALUES = {"production"}


class StubWitnessInProductionError(RuntimeError):
    """Raised when ``CAPSULE_WITNESS=stub`` and ``CAPSULE_ENV=production`` are
    both set. The stub witness proves nothing beyond self-attested (frozen
    surface §1a.4) -- shipping to production on it must never happen
    silently, so this is a hard, synchronous refusal, not a warning."""

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


def witness_mode(explicit: bool | None) -> str:
    """Resolve the three-way mode: ``"off"``, ``"on"`` (real witness), or
    ``"stub"`` (in-process, zero-network -- frozen surface §1a.4).

    ``explicit`` (the ``witness=`` kwarg) always wins when set -- ``True`` is
    ``"on"``, ``False`` is ``"off"``; there is no explicit-kwarg spelling of
    stub mode, only the env var (test/dev/CI posture, not a per-call
    choice). ``CAPSULE_WITNESS`` is consulted only when ``explicit`` is
    ``None``: any of ``_OFF_VALUES`` is ``"off"``, ``"stub"`` (case-
    insensitive) is ``"stub"``, anything else -- including unset -- is
    ``"on"``."""
    if explicit is not None:
        return "on" if explicit else "off"
    raw = os.environ.get(WITNESS_ENV_VAR, "").strip().lower()
    if raw in _OFF_VALUES:
        return "off"
    if raw in _STUB_VALUES:
        return "stub"
    return "on"


def witness_enabled(explicit: bool | None) -> bool:
    """Resolve the on/off decision: ``True`` whenever checkpoint mechanics
    should run at all -- i.e. mode is ``"on"`` OR ``"stub"`` (frozen surface
    §1a.4: the stub runs "the full mechanics ... against a local in-process
    stub"). Callers that need to distinguish real vs. stub use
    :func:`witness_mode` or :func:`witness_is_stub`."""
    return witness_mode(explicit) != "off"


def witness_is_stub(explicit: bool | None) -> bool:
    """``True`` iff the resolved mode is the in-process stub witness."""
    return witness_mode(explicit) == "stub"


def refuse_stub_in_production(explicit: bool | None) -> None:
    """Hard, synchronous refusal (frozen surface §1a.4): ``CAPSULE_WITNESS=stub``
    together with ``CAPSULE_ENV=production`` must never run -- "teams cannot
    ship to prod on stub without noticing." Raises
    :class:`StubWitnessInProductionError` immediately; never a warning, never
    silent. A no-op for every other mode/env combination, including
    production with a REAL witness (``CAPSULE_ENV=production`` alone is not
    an error -- only paired with stub).

    Called from two places, deliberately: ``core._emit_capsule()`` (before
    anything is written -- the primary, fail-fast path every ``seal()``/
    ``carry()``/``compose()`` call goes through) and :func:`maybe_checkpoint`
    itself (a safety net for a caller driving the checkpoint layer directly,
    per this module's docstring, without going through ``_emit_capsule``)."""
    if witness_mode(explicit) != "stub":
        return
    env = os.environ.get(CAPSULE_ENV_VAR, "").strip().lower()
    if env in _PRODUCTION_ENV_VALUES:
        raise StubWitnessInProductionError(
            f"capsule-emit: {WITNESS_ENV_VAR}=stub is set while {CAPSULE_ENV_VAR}=production -- "
            "refusing to run. The stub witness proves nothing beyond self-attested and must "
            "never ship to production. Fix one of: unset CAPSULE_WITNESS (uses the default "
            "hosted witness), set CAPSULE_WITNESS_URL to point at your own witness, or unset "
            "CAPSULE_ENV if this process is not actually production."
        )


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


def resolved_witness_urls(ts_url: str | list[str] | None = None) -> list[str]:
    """The witness endpoint(s) actually in effect, resolved with the exact
    same precedence :func:`maybe_checkpoint`'s caller (``core.emit()``/
    ``seal()``) already applies: an explicit ``ts_url`` wins; otherwise
    ``CAPSULE_WITNESS_URL``; otherwise the single free public-good default.

    Unlike :func:`_parse_witness_urls` (a pure normalizer that leaves "no
    override" as ``[]`` for its caller to fall back on), this always
    returns at least one URL -- callers outside a live ``emit()`` call (a
    retry pass, ``status``) that need to know "which witness(es) is this
    ledger actually configured against right now" have no other caller to
    fall back to.
    """
    from .checkpoint import DEFAULT_TS_URL

    if ts_url is None:
        ts_url = os.environ.get(WITNESS_URL_ENV_VAR)
    urls = _parse_witness_urls(ts_url)
    return urls or [DEFAULT_TS_URL]


_notice_lock = threading.Lock()
_notice_printed = False


def _print_first_use_notice_once(urls: list[str], *, stub: bool = False) -> None:
    """Print the one-time, first-use witness notice to stderr.

    Fires exactly once per process, at the first ``maybe_checkpoint()`` call
    where witnessing is enabled -- i.e. at the first ``seal()``, before any
    checkpoint has actually gone out over the network, not gated on the
    cadence counter reaching its threshold. Never raises; a broken stderr
    must not break emit().

    ``stub=True`` (``CAPSULE_WITNESS=stub``) prints the distinct scream this
    mode requires (frozen surface §1a.4: "the scream is everywhere the
    developer is: at the first stub-armed seal()...") instead of the normal
    witnessing notice -- it cannot be mistaken for the real thing, states
    plainly that nothing leaves the process, and names both exits (point at
    the hosted witness, or self-host one)."""
    global _notice_printed
    with _notice_lock:
        if _notice_printed:
            return
        _notice_printed = True
    try:
        if stub:
            print(
                "capsule-emit: STUB WITNESS is armed for this process (CAPSULE_WITNESS=stub) "
                "-- checkpoints will form and stamps will come back, but ZERO network is used "
                "and the grade never leaves self-attested; this mode proves nothing beyond "
                "self-attested to anyone but you. Never set CAPSULE_WITNESS=stub in production "
                "-- CAPSULE_ENV=production with stub set refuses to run. To get a real witness: "
                "unset CAPSULE_WITNESS to use the default hosted witness, or set "
                "CAPSULE_WITNESS_URL to point at your own. "
                "(This notice prints once per process.)",
                file=sys.stderr,
            )
            return
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

    def sign_cose_statement(
        self,
        payload: bytes,
        *,
        content_type: str,
        issuer: str,
        subject: str,
        extra_cwt_claims: dict | None = None,
    ) -> bytes:
        """Pass through to the wrapped ``signing.Signer``'s own
        ``sign_cose_statement`` ([cll-checkpoint-cose-wire]) -- so THIS
        adapter (what ``_build_and_register`` already holds as
        ``state.signer``) can also serve directly as the COSE-capable signer
        ``capsule_emit.checkpoint.cose_wire.checkpoint_to_cose`` needs,
        without a caller reaching past this adapter into the raw
        ``signing.Signer`` identity it wraps."""
        sign_cose_statement = getattr(self._signing_signer, "sign_cose_statement", None)
        if not callable(sign_cose_statement):
            raise TypeError(
                f"{type(self._signing_signer).__name__} cannot sign a COSE checkpoint statement"
            )
        return sign_cose_statement(
            payload,
            content_type=content_type,
            issuer=issuer,
            subject=subject,
            extra_cwt_claims=extra_cwt_claims,
        )


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


def _build_checkpoint_cose_hex(
    cp: Any,
    signer: Any,
    mmr: Any,
    prev_before: Any,
    consistency_proof: Any | None,
) -> str | None:
    """Best-effort COSE-wire serialization of ``cp``
    ([cll-checkpoint-cose-wire]) -- built HERE, at production time, because
    this is the one place the signing key AND the live MMR (``mmr``, for
    [cll-commitment-interop]'s conformant peak-list commitment) are both
    actually available; a later ``bundle()`` call may run keyless, in a
    different process, handed only the ledger file, so it can only ever
    READ this back, never mint it itself (see ``checkpoint.cose_wire``'s
    module docstring). Persisted alongside the JSON checkpoint in the stamp
    entry so ``bundle()`` can carry it straight through.

    Never raises into ``emit()``: a failure (e.g. the ``checkpoint`` extra's
    ``scitt-cose`` dependency not installed, or an ``mmr.peak_hashes_at``
    call failing) just means this checkpoint's wire form isn't available
    yet -- the JSON checkpoint and its own signature, verified
    independently, are unaffected. This is why ``mmr.peak_hashes_at`` is
    called IN HERE rather than by the caller: every step that touches
    [cll-commitment-interop]'s peak lists must stay inside this same
    try/except, not run unguarded before it.
    """
    try:
        from .checkpoint.cose_wire import checkpoint_to_cose

        new_peak_hashes = mmr.peak_hashes_at(cp.mmr_size)
        prev_peak_hashes = mmr.peak_hashes_at(prev_before.mmr_size) if prev_before is not None else None
        return checkpoint_to_cose(
            cp,
            signer,
            new_peak_hashes,
            prev_peak_hashes=prev_peak_hashes,
            consistency_proof=consistency_proof,
        ).hex()
    except Exception as exc:  # noqa: BLE001 -- best-effort, never raises into emit()
        warnings.warn(
            f"capsule-emit: COSE-wire checkpoint serialization for log_id={cp.log_id!r} "
            f"mmr_size={cp.mmr_size} failed (JSON checkpoint unaffected): {exc}",
            RuntimeWarning,
            stacklevel=1,
        )
        return None


def _persist_checkpoint_stamp(
    cp: Any, ledger_path: str, *, checkpoint_cose_hex: str | None = None
) -> None:
    """Write ``cp`` (with whatever ``WitnessRecord`` s it collected) back into
    its own ledger as a checkpoint-stamp entry -- see ``ledger.py``'s module
    docstring for the shape and why. Never called before ``cp.witnesses`` is
    final for this round: once written, the entry is immediately eligible to
    be folded into the MMR by the next ``state.mmr.sync()``, so writing it
    mid-registration could let a later witness append race an already-synced
    read of ``cp.witnesses`` elsewhere.

    ``checkpoint_cose_hex``, when supplied (see
    :func:`_build_checkpoint_cose_hex`), is carried as a SIBLING key
    (``checkpoint_cose``), never folded into ``cp.to_dict()`` -- it is
    outside what ``cp.entry_digest()`` commits to as this stamp's MMR leaf.
    That is deliberate, not an oversight: the COSE_Sign1 statement is
    already self-authenticating (its own Ed25519 signature, checked by
    :func:`capsule_emit.checkpoint.cose_wire.verify_checkpoint_cose_offline`),
    so it needs no additional MMR-leaf coverage, and leaving
    ``entry_digest()``'s covered shape unchanged keeps this an additive,
    backward-compatible field -- an older reader that has never heard of it
    still hashes the same leaf for this entry.

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
    if checkpoint_cose_hex is not None:
        entry["checkpoint_cose"] = checkpoint_cose_hex
    try:
        append_to_ledger(entry, ledger_path)
    except OSError as exc:  # noqa: BLE001 -- fire-and-forget, never raises into emit()
        warnings.warn(
            f"capsule-emit: failed to persist checkpoint stamp for log_id="
            f"{cp.log_id!r} mmr_size={cp.mmr_size} to {ledger_path!r}: {exc}",
            RuntimeWarning,
            stacklevel=1,
        )


# -- durable witness-outage retry queue --------------------------------------
#
# See the module docstring's "Witness outage" section. Everything below is a
# pure function of what's already durably on the ledger -- no separate
# in-memory or side-file queue, no persisted cursor to desync -- see that
# section for why that is deliberate, not an omission.


@dataclass(frozen=True)
class CheckpointWitnessState:
    """One persisted checkpoint stamp's EFFECTIVE witness set: its own
    ``witnesses`` (from the original registration attempt) plus whatever a
    later :class:`WITNESS_BACKFILL_KIND` entry added -- keyed by
    ``ts_url``, so a URL that both originally succeeded and was later
    (redundantly) backfilled still counts once. This is the merged view
    every consumer of witness state should read from -- never
    ``checkpoint.grade()``/``checkpoint.witnesses`` directly once backfills
    exist, since those only ever see the original registration."""

    entry_digest: str
    checkpoint: Any  # capsule_emit.checkpoint.CheckpointRecord
    effective_witnesses: dict  # ts_url -> capsule_emit.checkpoint.WitnessRecord

    def grade(self, *, ts_pubkey_pem: bytes | str | None = None) -> Any:  # -> capsule_emit.checkpoint.Grade
        """Same authenticity bar as ``CheckpointRecord.grade()`` -- a
        non-stub stamp that cryptographically verifies as bound to this
        checkpoint, not mere presence -- evaluated over the EFFECTIVE
        (backfill-merged) witness set instead of ``checkpoint.witnesses``
        alone, so a late backfilled stamp flips this to WITNESSED without
        needing the checkpoint's own, never-updated ``.witnesses`` list to
        change."""
        from .checkpoint import Grade, verify_witness_stamp_offline

        return (
            Grade.WITNESSED
            if any(
                not w.is_stub
                and verify_witness_stamp_offline(self.checkpoint, w, ts_pubkey_pem=ts_pubkey_pem)[0]
                for w in self.effective_witnesses.values()
            )
            else Grade.SELF_ATTESTED
        )


def checkpoint_witness_states(ledger_path: str) -> list[CheckpointWitnessState]:
    """Every checkpoint stamp in ``ledger_path``, in ledger order, each with
    its EFFECTIVE witness set (original registration + any later backfill
    merged) -- see :class:`CheckpointWitnessState`. One full scan of the
    ledger; callers that need more than one view of this (e.g. ``status``,
    which needs both the latest checkpoint's grade and every witness's
    backlog) should call this once and derive both, rather than scanning
    the ledger twice.
    """
    from .checkpoint import CheckpointRecord, WitnessRecord
    from .ledger import CHECKPOINT_STAMP_KIND, WITNESS_BACKFILL_KIND, read_ledger_entries

    checkpoints: dict[str, Any] = {}
    order: list[str] = []
    # entry_digest -> {ts_url: WitnessRecord}, accumulated in ledger order so
    # a later backfill for a URL a checkpoint already held simply overwrites
    # with an equal-or-newer record rather than duplicating.
    backfills: dict[str, dict[str, Any]] = {}

    for entry in read_ledger_entries(ledger_path):
        kind = entry.get("kind")
        if kind == CHECKPOINT_STAMP_KIND:
            digest = entry["capsule_id"]
            checkpoints[digest] = CheckpointRecord.from_dict(entry["checkpoint"])
            order.append(digest)
        elif kind == WITNESS_BACKFILL_KIND:
            wr = WitnessRecord.from_dict(entry["witness"])
            backfills.setdefault(entry["checkpoint_entry_digest"], {})[wr.ts_url] = wr

    states = []
    for digest in order:
        cp = checkpoints[digest]
        effective = {w.ts_url: w for w in cp.witnesses}
        effective.update(backfills.get(digest, {}))
        states.append(CheckpointWitnessState(digest, cp, effective))
    return states


def checkpoint_witness_backlog(ledger_path: str, ts_urls: list[str]) -> dict[str, list]:
    """For each ``ts_urls`` entry, the checkpoints (as ``CheckpointRecord``
    objects, oldest first) that witness has not yet confirmed -- neither
    originally nor via a later backfill. This IS the durable queue: derived
    fresh from the ledger every call, so it is exactly as durable as the
    ledger itself and cannot grow unboundedly in process memory (nothing
    here is retained between calls)."""
    states = checkpoint_witness_states(ledger_path)
    return {
        url: [s.checkpoint for s in states if url not in s.effective_witnesses]
        for url in ts_urls
    }


def _persist_witness_backfill(cp: Any, witness_record: Any, ledger_path: str) -> None:
    """Record a witness stamp that arrived after ``cp`` was first persisted
    -- see the module docstring's "Witness outage" section for why this is
    a new entry rather than a mutation of ``cp``'s own stamp. Best-effort,
    matching :func:`_persist_checkpoint_stamp`: a failure to persist a
    backfill must not raise into a caller -- the stamp itself (and the COSE
    receipt already obtained from the TS) is unaffected; only this ledger's
    own record of having obtained it is at risk, and it will simply be
    retried on the next drain.
    """
    from .ledger import WITNESS_BACKFILL_KIND, append_to_ledger

    body = {
        "checkpoint_entry_digest": cp.entry_digest(),
        "witness": witness_record.to_dict(),
    }
    entry = {
        "kind": WITNESS_BACKFILL_KIND,
        "v": 1,
        "capsule_id": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        **body,
    }
    try:
        append_to_ledger(entry, ledger_path)
    except OSError as exc:  # noqa: BLE001 -- best-effort, never raises into a caller
        warnings.warn(
            f"capsule-emit: failed to persist witness backfill for checkpoint "
            f"entry_digest={cp.entry_digest()!r} ts_url={witness_record.ts_url!r} "
            f"to {ledger_path!r}: {exc}",
            RuntimeWarning,
            stacklevel=1,
        )


def retry_pending_witness_stamps(
    ledger_path: str,
    *,
    ts_url: str | list[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, int]:
    """Drain each configured witness's durable backlog -- the per-witness
    cursor described in the module docstring. For every witness in
    :func:`resolved_witness_urls`, attempts to register its oldest pending
    checkpoint first, then the next, stopping at that witness's first
    failure this call (it is presumably still down; nothing here is lost --
    the next call, whether from the next real ``emit()``/``seal()`` crossing
    cadence or another explicit call, re-derives the same backlog from the
    ledger and resumes at the same point). One witness's backlog draining
    (or not) never affects another's -- each URL's loop is independent.

    Gated by :func:`witness_enabled` exactly like :func:`maybe_checkpoint`
    (O16-03: the kill switch is a single, absolute zero-egress guarantee --
    a retry pass must honor it too, not just the original registration
    attempt). Synchronous -- callers that want this off the calling thread
    (``maybe_checkpoint``'s dispatched worker) call it from there.

    Returns ``{ts_url: count_backfilled_this_call}``.
    """
    from .checkpoint import register_checkpoint

    if not witness_enabled(enabled):
        return {}

    urls = resolved_witness_urls(ts_url)
    backlog = checkpoint_witness_backlog(ledger_path, urls)
    backfilled = {url: 0 for url in urls}
    for url, pending in backlog.items():
        for cp in pending:
            try:
                witness_record = register_checkpoint(cp, url)
            except Exception:  # noqa: BLE001 -- still down; stop this witness's drain for now
                break
            _persist_witness_backfill(cp, witness_record, ledger_path)
            backfilled[url] += 1
    return backfilled


def _build_and_register(state: _WitnessState, ts_urls: list[str], *, stub: bool = False) -> None:
    from .checkpoint import (
        DEFAULT_TS_URL,
        STUB_TS_URL,
        CheckpointError,
        RollbackError,
        emit_checkpoint,
        register_checkpoint,
        register_checkpoint_stub,
    )

    # In stub mode, never label a stamp with a real-looking endpoint (a
    # configured CAPSULE_WITNESS_URL, or the real hosted default) -- nothing
    # is actually dialed, so the label must say so plainly (STUB_TS_URL),
    # not borrow a URL that would read as "this really reached that host."
    resolved_urls = [STUB_TS_URL] if stub else (ts_urls or [DEFAULT_TS_URL])

    # Drain each configured witness's durable backlog BEFORE handling the
    # checkpoint newly due this cycle -- oldest pending stamp first, per
    # witness, so a witness that just came back resumes from its own
    # cursor instead of only ever seeing the newest checkpoint. Reads/
    # appends only already-settled checkpoint stamps on disk, never
    # ``state.mmr``/``state.prev``, so it needs no lock here.
    retry_pending_witness_stamps(state.log_id, ts_url=resolved_urls)

    with state.lock:
        state.mmr.sync()
        prev_before = state.prev
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
        # Built inside the lock, right after `cp`, while `state.mmr` is
        # still guaranteed to hold every node `consistency_proof`/
        # `peak_hashes_at` needs -- see `_build_checkpoint_cose_hex`'s
        # docstring for why this is the only place the checkpoint's wire
        # form can be minted at all.
        consistency_proof = (
            state.mmr.consistency_proof(prev_before.mmr_size, cp.mmr_size)
            if prev_before is not None
            else None
        )
        checkpoint_cose_hex = _build_checkpoint_cose_hex(
            cp, state.signer, state.mmr, prev_before, consistency_proof
        )

    # Fan the same checkpoint out to every endpoint independently -- one
    # endpoint failing must never block registration with the others. In
    # stub mode this never dials out (register_checkpoint_stub is local,
    # zero-network) but still runs once per named URL (or once, unlabeled,
    # if none were given) so the fan-out shape matches the real path exactly
    # -- the point of the stub is to exercise the real code, not a shortcut
    # around it.
    for url in resolved_urls:
        try:
            witness_record = (
                register_checkpoint_stub(cp, url) if stub else register_checkpoint(cp, url)
            )
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
    _persist_checkpoint_stamp(cp, state.log_id, checkpoint_cose_hex=checkpoint_cose_hex)


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

    Raises :class:`StubWitnessInProductionError` immediately (before the
    notice, before anything else) if ``CAPSULE_WITNESS=stub`` and
    ``CAPSULE_ENV=production`` are both set -- see
    :func:`refuse_stub_in_production`. This is a safety net for a caller
    driving this function directly; ``core._emit_capsule()`` already checks
    before this point is ever reached.
    """
    mode = witness_mode(enabled)
    if mode == "off":
        return
    refuse_stub_in_production(enabled)
    is_stub = mode == "stub"

    urls = _parse_witness_urls(ts_url)
    _print_first_use_notice_once(urls, stub=is_stub)

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
            _build_and_register(state, urls, stub=is_stub)
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
