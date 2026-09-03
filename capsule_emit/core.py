# SPDX-License-Identifier: Apache-2.0
"""capsule-emit core — the internal capsule-construction primitive.

This is the adoption-surface API described in capsule-emit-quickstart.md.
It wraps ``agent_action_capsule.emit()`` with:
- A friendlier signature (action, operator, developer, agent_input, agent_output, model, verdict, effect)
- Digest-only commitment of agent_input / agent_output (content stays local)
- Optional per-emit digest salting (``salt_digests=True``), for a privacy-sensitive
  deployment that wants cross-capsule input correlation resistance
- Async checkpoint/witness on by default, lazy per ledger (checkpoint-only,
  never capsule content; see ``capsule_emit.witness`` and
  ``docs/checkpoint.md``) — the only default egress channel as of 0.5.0
  (see "Single egress" below)
- Every capsule cryptographically signed over its ``capsule_id`` by a
  persisted producer key -- self-attested strength, by default, on every
  call (see ``capsule_emit.signing``)
- Automatic JSONL ledger append
- A typed EmitResult with .capsule_id, .anchored, .anchor_status, .signature,
  and .key_id (.anchored / .anchor_status report the legacy, non-default
  anchor channel — see below)

**Single egress (2026-08, O16 items 1-2):** the per-seal SCITT anchor
submission that used to dispatch on every ``seal()``/``received()`` call by
default has been killed as a default. The checkpoint/witness stream
is now the only default network path. The old anchor channel still exists as
an explicit, non-default opt-in — pass ``anchor=True``, or set
``CAPSULE_ANCHOR=legacy-on`` (a deliberately distinct value from the old
on-values, so an existing ``CAPSULE_ANCHOR=true`` config does not silently
keep double-egress alive across the upgrade) — kept only as a rollback path
for one release. **Even when opted back in, the legacy channel stays subject
to the witness kill switch (O16-03)** — ``witness=False`` /
``CAPSULE_WITNESS=off`` is the one switch that zeroes ALL egress, anchor
included. See ``docs/why-anchoring.md`` and ``docs/checkpoint.md``.

The ``confirms`` parameter threads a "did → confirmed" chain without a scheduler.

The same ``_emit_capsule()`` calls and ledger files are compatible with gateway
layers that enforce declared manifests — no code changes required to add
enforcement on top.

**Attestation honesty (spec §3.2 MUST):** ``anchored`` is reported ONLY when a
real ``AnchorResult`` confirms the submission — never merely because anchoring
was requested. The default (non-blocking) anchor path can only ever report the
weaker ``anchor_status="submitted"``; pass ``anchor_wait=<seconds>`` to block
for a real confirmed/failed outcome. See ``transparent.py`` / ``verify.py`` /
``cli.py`` in ``agent_action_capsule`` for the same rule enforced elsewhere.

**Clean break (2026-08-22):** the public developer verb ``emit()`` (importable
from the top-level ``capsule_emit`` package) is now a raising stub — use
``seal()``/``received()`` (see ``capsule_emit.surface``) instead. **Clean
break (2026-08-27):** ``compose()``/``carry()``, the v3 flat-bind verbs, are
gone from the public surface too — the slot-form (``seal(who(...),
can(...), ...)``) supersedes ``compose()``, and ``carry()``'s body was
already ``received()``'s. This module's ``_emit_capsule()`` is the
fully-parameterized primitive those verbs (and the framework adapters)
wrap; it is internal, not a deprecated public verb, and callers needing
every keyword in one call should import it explicitly rather than reach for
the removed ``emit()``.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from agent_action_capsule import emit as _base_emit
from agent_action_capsule.anchor import AnchorError, AnchorFuture, AnchorResult, async_anchor
from agent_action_capsule.canonical import jcs, json_digest, normalize
from agent_action_capsule.contracts import Disposition, EffectRecord, InvariantError

from . import signing as _signing
from . import witness as _witness
from .canonicalization import compute_capsule_id
from .ledger import append_to_ledger
from .numbers import CANONICALIZATION_ID
from .signing import Signer

__all__ = ["_emit_capsule", "EmitResult"]

_DEFAULT_LEDGER = "ledger.jsonl"

AnchorStatus = Literal["confirmed", "submitted", "failed", "skipped"]

#: How long the atexit handler blocks, in total, joining outstanding anchor
#: futures before giving up and warning. Overridable for tests.
_ATEXIT_ANCHOR_TIMEOUT = float(
    os.environ.get("CAPSULE_EMIT_ATEXIT_ANCHOR_TIMEOUT", "5.0")
)

#: Explicit ``anchor=`` always wins; this env var is consulted only when the
#: caller leaves ``anchor`` at its default (``None``). Unlike
#: ``capsule_emit.witness.WITNESS_ENV_VAR`` (which defaults ON), this
#: defaults OFF as of 0.5.0 (O16 items 1-2: the per-seal anchor channel is
#: killed as a default egress path) — only the exact value ``"legacy-on"``
#: re-enables it, kept as a one-release rollback escape hatch. Pre-0.5.0
#: on-values (``"true"``/``"1"``/``"yes"``/unset) no longer enable anchor —
#: an existing ``CAPSULE_ANCHOR=true`` config silently downgrades to
#: single-egress (checkpoint-only) rather than continuing double-egress.
#:
#: **O16-03: this switch alone is not sufficient to enable the channel.**
#: The result of :func:`_anchor_enabled` is further gated by the witness
#: kill switch at the ``_emit_capsule`` call site -- ``witness=False`` /
#: ``CAPSULE_WITNESS=off`` disables the legacy anchor channel too, even when
#: ``CAPSULE_ANCHOR=legacy-on`` / ``anchor=True`` is explicitly set. See
#: ``docs/checkpoint.md``'s "Kill switch scope" section.
ANCHOR_ENV_VAR = "CAPSULE_ANCHOR"
_ANCHOR_LEGACY_ON_VALUE = "legacy-on"

#: The pre-0.5.0 on-values. Kept solely to detect the O16-01-02 breaking
#: change at runtime: a caller who wrote one of these before the flip now
#: gets a silent no-op (single-egress) instead of the anchor channel they
#: asked for, with nothing in the docs surfacing that unless they read them
#: again. See ``_print_legacy_anchor_env_stale_notice_once`` below.
_ANCHOR_STALE_ON_VALUES = {"true", "1", "yes"}

_stale_anchor_notice_lock = threading.Lock()
_stale_anchor_notice_printed = False


def _print_legacy_anchor_env_stale_notice_once(value: str) -> None:
    """One-time stderr notice for the O16-01-02 breaking change: a pre-0.5.0
    affirmative ``CAPSULE_ANCHOR`` value (``true``/``1``/``yes``) used to
    enable the per-seal anchor channel; as of 0.5.0 it is off by default and
    that value is now silently ignored. Never raises; a broken stderr must
    not break ``_emit_capsule()``."""
    global _stale_anchor_notice_printed
    with _stale_anchor_notice_lock:
        if _stale_anchor_notice_printed:
            return
        _stale_anchor_notice_printed = True
    try:
        print(
            f"capsule-emit: CAPSULE_ANCHOR={value!r} used to enable the per-seal "
            "anchor channel, but as of 0.5.0 that channel is off by default and "
            "this value is now a no-op -- your anchor config is silently NOT "
            "taking effect (single-egress: checkpoint/witness only). Set "
            "CAPSULE_ANCHOR=legacy-on to restore the old behavior. (This notice "
            "prints once per process.)",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 -- a notice must never break _emit_capsule()
        pass


def _anchor_enabled(explicit: bool | None) -> bool:
    """Resolve the on/off decision for the legacy per-seal anchor channel,
    from the ``CAPSULE_ANCHOR``/``anchor=`` axis alone.

    ``explicit`` (the ``anchor=`` kwarg) always wins when set. Otherwise the
    channel stays off unless ``CAPSULE_ANCHOR=legacy-on`` — the deliberately
    narrow escape hatch described on ``ANCHOR_ENV_VAR`` above. Callers must
    additionally AND this with the witness kill switch (see
    ``ANCHOR_ENV_VAR``'s O16-03 note) -- this function alone does not apply
    it."""
    if explicit is not None:
        return explicit
    raw = os.environ.get(ANCHOR_ENV_VAR, "").strip().lower()
    if raw in _ANCHOR_STALE_ON_VALUES:
        _print_legacy_anchor_env_stale_notice_once(raw)
    return raw == _ANCHOR_LEGACY_ON_VALUE


_disclosure_lock = threading.Lock()
_disclosure_printed = False


def _print_first_run_disclosure_once(
    *, anchor_active: bool, witness_active: bool, anchor_endpoint: str | None, witness_endpoint: str | None
) -> None:
    """Print the one-time, first-call network disclosure to stderr.

    Covers BOTH default-on network paths (anchor submission + witness
    checkpoint) in a single notice, printed synchronously in the calling
    thread — before ``async_anchor()`` is dispatched and before
    ``_witness.maybe_checkpoint()`` can register a checkpoint — so no network
    I/O happens in this process ahead of the disclosure. A call where both
    paths are disabled (``anchor=False`` and effectively no witnessing) never
    triggers a network attempt, so it prints nothing. Never raises; a broken
    stderr must not break ``_emit_capsule()``."""
    if not (anchor_active or witness_active):
        return
    global _disclosure_printed
    with _disclosure_lock:
        if _disclosure_printed:
            return
        _disclosure_printed = True
    lines = [
        "capsule-emit: before this process's first network attempt, here is exactly what leaves:"
    ]
    if anchor_active:
        display = anchor_endpoint or "the default anchor endpoint (AAC_ANCHOR_URL unset)"
        lines.append(
            f"  - ANCHOR: (legacy, non-default channel — explicitly opted into for this "
            f"process) the capsule_id (a 64-char hex SHA-256 digest — no business "
            f"content) is submitted to {display} on every seal()/received() "
            "call. Disable with anchor=False, or unset CAPSULE_ANCHOR / remove "
            "CAPSULE_ANCHOR=legacy-on."
        )
    if witness_active:
        display = witness_endpoint or "the default witness endpoint (CAPSULE_WITNESS_URL unset)"
        lines.append(
            f"  - WITNESS: a signed checkpoint of the log (its size, a root hash, a "
            f"timestamp — no capsule content) is POSTed to {display} at its "
            "/checkpoints route once enough ledger entries accumulate. Disable "
            "with witness=False or CAPSULE_WITNESS=off."
        )
    lines.append(
        "Both are content-free — no capsule content ever leaves the process. "
        "(This notice prints once per process.)"
    )
    try:
        print("\n".join(lines), file=sys.stderr)
    except Exception:  # noqa: BLE001 -- a notice must never break _emit_capsule()
        pass


_anchor_deps_lock = threading.Lock()
_anchor_deps_checked = False
_anchor_deps_available = True

_dep_notice_lock = threading.Lock()
_dep_notice_printed = False


def _anchor_dependency_available() -> bool:
    """Cheap, cached, one-time check that the optional SCITT anchor stack
    (the ``agent-action-capsule[anchor]`` extra: ``scitt_cose`` +
    ``cryptography``) is importable — the same imports ``submit_anchor``
    makes internally. Checked once per process, synchronously, at the moment
    of the first anchor attempt, so a missing dependency is reported plainly
    and immediately rather than depending on the background worker failing
    and the atexit sweep still finding its future pending later."""
    global _anchor_deps_checked, _anchor_deps_available
    with _anchor_deps_lock:
        if _anchor_deps_checked:
            return _anchor_deps_available
        _anchor_deps_checked = True
        try:
            import scitt_cose.statement  # noqa: F401
            from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: F401,F811
        except ImportError:
            _anchor_deps_available = False
        return _anchor_deps_available


def _print_missing_anchor_dependency_notice_once() -> None:
    """Plain, one-time stderr notice for the case the atexit RuntimeWarning
    used to report only cryptically (a raw ``repr(ModuleNotFoundError(...))``)
    and only for whichever anchor futures happened to still be pending at
    interpreter shutdown — most were silently swept away well before then.
    Printed once per process regardless of how many ``seal()``/``received()``
    calls hit the same missing dependency."""
    global _dep_notice_printed
    with _dep_notice_lock:
        if _dep_notice_printed:
            return
        _dep_notice_printed = True
    try:
        print(
            "capsule-emit: anchor is on by default but the optional SCITT anchor "
            "dependency isn't installed (pip install 'agent-action-capsule[anchor]') "
            "-- anchor submissions in this process cannot succeed. Disable with "
            "anchor=False or CAPSULE_ANCHOR=off. (This notice prints once per process.)",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 -- a notice must never break _emit_capsule()
        pass


_pending_anchors_lock = threading.Lock()
# capsule_id -> (AnchorFuture, endpoint-or-None)
_pending_anchors: dict[str, tuple[AnchorFuture, str | None]] = {}


def _track_pending_anchor(capsule_id: str, future: AnchorFuture, endpoint: str | None) -> None:
    """Track a newly-submitted anchor future, sweeping completed ones first.

    ``AnchorFuture`` has no completion callback (only non-blocking ``.done()``
    and blocking ``.result(timeout=)``), so this opportunistic sweep — run on
    every new submission — is what reclaims memory on the default
    non-blocking path. ``.done()`` goes True on both the success path and the
    internal-exception path inside ``async_anchor``'s worker thread, so this
    is correct for anchor failures too, unlike relying on ``on_result``
    (which ``agent_action_capsule`` 0.1.0 only invokes on success). This
    bounds growth to "entries between the last two _emit_capsule() calls," not
    "forever" — a capsule anchored right before the process goes idle with no
    further _emit_capsule() calls still relies on the atexit handler as the backstop.
    """
    with _pending_anchors_lock:
        for pending_id, (pending_future, _) in list(_pending_anchors.items()):
            if pending_future.done():
                del _pending_anchors[pending_id]
        _pending_anchors[capsule_id] = (future, endpoint)


def _untrack_pending_anchor(capsule_id: str) -> None:
    with _pending_anchors_lock:
        _pending_anchors.pop(capsule_id, None)


def _join_pending_anchors_at_exit() -> None:
    """Join outstanding anchor futures with a bounded shared timeout.

    Never a silent-success path: futures that time out or resolve to an
    ``AnchorError`` get a ``RuntimeWarning`` naming the capsule_id and the
    endpoint. Futures that resolve to a real ``AnchorResult`` are silently
    dropped — that is a genuine success, not a claim we have to hedge.
    """
    with _pending_anchors_lock:
        pending = list(_pending_anchors.items())
    if not pending:
        return
    deadline = time.monotonic() + _ATEXIT_ANCHOR_TIMEOUT
    for capsule_id, (future, endpoint) in pending:
        remaining = max(0.0, deadline - time.monotonic())
        result = future.result(timeout=remaining)
        if result is None:
            display_endpoint = endpoint or "the default anchor endpoint (AAC_ANCHOR_URL unset)"
            warnings.warn(
                f"capsule-emit: anchor submission for capsule_id={capsule_id!r} to "
                f"{display_endpoint} did not complete before interpreter shutdown "
                f"(joined for {_ATEXIT_ANCHOR_TIMEOUT}s) — outcome unknown, "
                "the transparency log may not contain this capsule.",
                RuntimeWarning,
                stacklevel=1,
            )
        elif isinstance(result, AnchorError):
            # A missing optional dependency was already reported once, plainly,
            # by _print_missing_anchor_dependency_notice_once() at dispatch time
            # (see _anchor_dependency_available()) — repeating it here per
            # capsule_id, as a raw ModuleNotFoundError repr, is the cryptic
            # per-call noise this notice replaces, not a second genuine fact.
            if not (_anchor_deps_checked and not _anchor_deps_available):
                warnings.warn(
                    f"capsule-emit: anchor submission for capsule_id={capsule_id!r} to "
                    f"{result.ts_url} failed: {result.error}",
                    RuntimeWarning,
                    stacklevel=1,
                )
        _untrack_pending_anchor(capsule_id)


atexit.register(_join_pending_anchors_at_exit)


def _digest(value: Any, salt: str | None = None) -> str:
    """SHA-256 over the RFC 8785 (JCS) canonicalization of ``value``.

    Uses the same ``json_digest`` (JCS) as :func:`verify_input_digest`, so a
    faithfully-sealed ``agent_input`` / ``agent_output`` re-verifies.

    Before 0.3.2 this used ``json.dumps(sort_keys=True)``, which diverged from
    the JCS verifier for any value containing a null, an empty container, or a
    non-ASCII field — a faithfully-sealed such receipt then failed
    :func:`verify_input_digest` (returned ``False``). JCS now closes that gap.

    Floats fail closed: a raw JSON float is a §5.1 error (it cannot be
    reproducibly digested), so ``_emit_capsule()`` raises ``FloatInDigestError`` here
    rather than silently sealing an ``agent_input`` / ``agent_output`` that
    :func:`verify_input_digest` could never confirm. Encode monetary/quantity
    values as exact decimal strings before sealing.

    Non-JSON-native types the legacy encoder tolerated via ``default=str``
    (e.g. tuples, arbitrary objects) still fall back to the legacy sorted-key
    encoding, so those emitters do not break at seal time.

    When *salt* is given, it is folded into the exact JCS pre-image bytes
    (``jcs_bytes + b"|" + salt``) before hashing — the salted digest is over
    the same canonicalization as the unsalted one, just committing an extra
    salt suffix, so opting into ``salt_digests=True`` never reintroduces the
    pre-0.3.2 JCS/sort_keys divergence this function's own history already
    fixed.
    """
    try:
        if salt is None:
            return json_digest(value)
        # jcs(normalize(value)) raises TypeError on the same non-JSON-native
        # inputs json_digest would, so this falls through to the identical
        # fallback branch below on those inputs.
        canonical_bytes = jcs(normalize(value))
        return hashlib.sha256(canonical_bytes + b"|" + salt.encode("utf-8")).hexdigest()
    except TypeError:
        # Non-JSON-native types (e.g. tuples, arbitrary objects) — tolerated as
        # before. FloatInDigestError is intentionally NOT caught: a raw float is
        # a spec-defined error, so _emit_capsule() fails closed at the door.
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        if salt is not None:
            raw = raw + "|" + salt
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class EmitResult:
    """The result of a capsule-emit _emit_capsule() call.

    ``anchored`` / ``anchor_status`` report the legacy, non-default anchor
    channel (see ``ANCHOR_ENV_VAR`` / ``_anchor_enabled`` above) — as of
    0.5.0 the checkpoint/witness stream is the only default egress path, so
    for the overwhelming majority of calls ``anchor_status`` is
    ``"skipped"``. These fields only become meaningful when the legacy
    channel was explicitly opted into (``anchor=True`` or
    ``CAPSULE_ANCHOR=legacy-on``).

    ``anchored`` is ``True`` ONLY when a real ``AnchorResult`` confirmed the
    submission (i.e. ``anchor_wait`` was set and the future resolved to a
    success before the wait elapsed). The default non-blocking anchor path
    never sets ``anchored=True`` — it cannot know the outcome yet. Use
    ``anchor_status`` for the weaker "was it submitted" fact:

    - ``"confirmed"`` — a real ``AnchorResult`` was obtained (``anchored`` is True).
    - ``"submitted"`` — the anchor POST was dispatched but the outcome is not
      yet known (the background thread is still in flight, or
      ``anchor_wait`` timed out before a result arrived).
    - ``"failed"`` — a real ``AnchorError`` was obtained (only observable when
      ``anchor_wait`` is set; otherwise a background failure surfaces only via
      the ``atexit`` warning at interpreter shutdown).
    - ``"skipped"`` — the legacy anchor channel was never engaged: either
      ``anchor=False`` was passed, or it was left unset with no
      ``CAPSULE_ANCHOR=legacy-on`` opt-in (the default as of 0.5.0).

    ``signature`` / ``key_id`` mirror ``capsule["signature"]`` /
    ``capsule["key_id"]`` — the self-attested producer proof over
    ``capsule_id`` and the producer key that made it. ``signature`` is a
    hex-encoded COSE_Sign1 envelope (the frozen AAC producer-envelope
    profile, [capsule-cose-sign1]), not a bare signature; ``key_id`` is the
    raw Ed25519 public key, hex (see ``capsule_emit.signing``). Always
    present; every ``EmitResult`` is signed, not just anchored/witnessed
    ones.

    ``seq`` is this capsule's 1-indexed position in its ledger file (see
    ``capsule_emit.ledger.append_to_ledger``) — the log is where every
    capsule already lives ambiently, per the frozen surface's "already a
    leaf in your log" (§2.1): once a checkpoint covers this position, ``seq``
    is the MMR leaf index too. Rendered as ``#logged @ leaf <seq>`` by
    ``__repr__`` and by ``ledger.show()``.
    """

    capsule_id: str
    anchored: bool
    capsule: dict
    anchor_status: AnchorStatus
    signature: str
    key_id: str
    seq: int

    def __repr__(self) -> str:
        return (
            f"EmitResult(capsule_id={self.capsule_id!r}, anchored={self.anchored}, "
            f"anchor_status={self.anchor_status!r}) #logged @ leaf {self.seq}"
        )


def _emit_capsule(
    action: str,
    operator: str = "",
    developer: str = "",
    *,
    runtime: str | None = None,
    agent_input: Any = None,
    agent_output: Any = None,
    model: dict[str, str] | None = None,
    verdict: str = "executed",
    effect: dict[str, Any] | None = None,
    confirms: str | None = None,
    relation: str | None = "confirms",
    anchor: bool | None = None,
    ledger: str | os.PathLike = _DEFAULT_LEDGER,
    anchor_url: str | None = None,
    anchor_wait: float | None = None,
    witness: bool | None = None,
    witness_url: str | list[str] | None = None,
    human_disposed: bool = False,
    approver: str = "policy",
    decision: str = "accept",
    action_type: str | None = None,
    extra_compute: dict[str, Any] | None = None,
    disposition_authority: str | None = None,
    salt_digests: bool = False,
    canonicalization_id: str = CANONICALIZATION_ID,
    signer: Signer | None = None,
    signing_key_path: str | os.PathLike | None = None,
) -> EmitResult:
    """Emit a sealed, optionally anchored Agent Action Capsule.

    Args:
        action: A short, stable action name (e.g. ``"write_po"``).
        operator: Tenant / org identifier.
        developer: Agent name + version (e.g. ``"po-agent@v1"``).
        runtime: Framework hint (e.g. ``"langchain"``); stored in compute_attestation.
        agent_input: The agent's input (any JSON-serializable value). Digest-committed;
            the raw value never leaves the process.
        agent_output: The agent's output. Digest-committed.
        model: Dict with ``"provider"`` and ``"model_id"`` keys.
        verdict: Disposition verdict_class (e.g. ``"executed"``, ``"confirmed"``).
        effect: Effect dict with ``"type"`` and ``"status"`` (and optional ``"autonomy"``).
        confirms: capsule_id of the prior capsule this one chains to.
        relation: Chain relation (``"confirms"`` | ``"supersedes"`` | ``"escalates"``
            | ``"assesses"`` | ``"adjudicates"`` | …), or ``None`` to keep the chain
            link (``confirms``) without asserting a relation value on it — e.g. a
            human refusal that chains to the capsule it denies without claiming to
            "confirm" it. ``"assesses"`` is for a judge/verdict capsule that cites a
            subject capsule by digest without confirming its outcome (a detection
            relation, never an enforcement one). ``"adjudicates"`` is for a
            twin-comparison referee capsule that cites two compared halves and
            records a ``corroborated``/``inconclusive``/``contradicted:<owner_id>``
            verdict — see :mod:`capsule_emit.adjudication`. Passing a non-``None``,
            non-default relation without ``confirms`` set raises ``ValueError``
            (a chain relation needs a chain target); ``relation=None`` never
            raises regardless of ``confirms``. Default ``"confirms"``.
        anchor: Legacy, non-default channel — killed as a default in 0.5.0 (O16
            items 1-2). ``None`` (default) never dispatches. Pass ``True``, or
            set ``CAPSULE_ANCHOR=legacy-on``, to opt back into the old
            per-seal, async, digest-only SCITT anchor submission
            (:func:`agent_action_capsule.anchor.async_anchor`) — kept only as
            a one-release rollback path (see ``docs/why-anchoring.md``). Non-
            blocking by default — see ``anchor_wait`` to block for a confirmed
            outcome, and ``EmitResult.anchor_status`` for the non-blocking
            outcome signal. Pass ``False`` (or leave unset) to keep it off; an
            explicit ``anchor=`` kwarg always overrides the env var. A
            first-run notice (see ``_print_first_run_disclosure_once``)
            prints to stderr before this process's first anchor or witness
            network attempt, naming the endpoint(s) and how to disable them.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor_url: Override the anchor endpoint (else reads ``AAC_ANCHOR_URL`` env var).
        anchor_wait: When set, block up to this many seconds for the anchor
            submission to resolve, and report the real outcome via
            ``EmitResult.anchored`` / ``.anchor_status``. When ``None`` (default),
            ``_emit_capsule()`` never blocks and ``anchored`` is always ``False`` (the
            submission's outcome is not yet known at return time).
        witness: When ``True`` (default, unless overridden — see below),
            this ledger participates in the default CLL checkpoint/witness
            stream: once enough entries accumulate (see
            ``capsule_emit.witness.DEFAULT_CADENCE_ENTRIES``) or enough time
            has passed since the first unwitnessed entry (see
            ``capsule_emit.witness.DEFAULT_CADENCE_SECONDS`` — 15 minutes,
            whichever comes first; never fires on age alone with no
            unwitnessed work), a signed peaks checkpoint over this ledger's
            MMR is built and registered with a Transparency Service, at its
            ``/checkpoints`` route — async, checkpoint-only (never capsule
            content), lazy (nothing is imported or computed until a
            checkpoint is actually due; see ``capsule_emit.witness`` and
            ``docs/checkpoint.md``). Pass
            ``False`` to opt this ledger out, or set ``CAPSULE_WITNESS=off``
            to opt out everywhere without a code change (an explicit
            ``witness=`` kwarg always overrides the env var). Never blocks —
            there is no ``witness_wait`` because a checkpoint reports on a
            *stream*, not this one call. For test/dev/CI, ``CAPSULE_WITNESS=stub``
            runs the identical mechanics against a local, zero-network stub —
            the grade never leaves self-attested (see
            ``capsule_emit.witness.witness_mode`` and
            ``docs/checkpoint.md``'s "Test & dev" section). **Refuses to run**
            (``capsule_emit.witness.StubWitnessInProductionError``) if
            ``CAPSULE_ENV=production`` is also set — stub must never ship to
            production silently.
        witness_url: Override the witness Transparency Service endpoint(s)
            (else reads ``CAPSULE_WITNESS_URL`` env var, else the free
            public-good tier at ``witness.agentactioncapsule.org`` --
            currently served via ``anchor.agentactioncapsule.org`` while its
            CNAME is pending). Pass a single URL, or several (a list, or a
            comma-separated string for the env var) to register the same
            checkpoint with more than one Transparency Service at once --
            what climbs from *witnessed (single witness)* to *multi-witness,
            equivocation-resistant* (see ``docs/checkpoint.md``).
        human_disposed: Whether a human made the disposition decision. When True,
            ``approver`` MUST be ``"human"`` — raises ``ValueError`` otherwise.
        approver: Who approved the disposition: ``"human"`` or ``"policy"`` (default).
        decision: Disposition decision string (default ``"accept"``).
        action_type: ``"decide"`` | ``"act"`` | ``"retrieve"`` | ``"fyi"`` override.
            When ``None`` (default), auto-derived from *verdict* — disposition verbs
            (``"executed"``, ``"confirmed"``, ``"denied"``, ``"blocked"``,
            ``"assessed"``) map to ``"decide"``; anything else maps to ``"fyi"``.
            ``"assessed"`` is a disposition class for judge/verdict capsules —
            it is detection, never enforcement, and must never be conflated
            with ``"executed"``/``"confirmed"``.
        extra_compute: Extra key/value pairs merged into ``compute_attestation``.
            Use for framework-specific context (MCP request ID, host info, etc.).
        disposition_authority: Opaque grant reference stored in
            ``disposition.authority`` (e.g. an AAuth JTI). Never the token body —
            only the stable identifier that lets a verifier confirm the authorization
            out-of-band.
        salt_digests: When ``True``, a fresh random 16-byte hex salt is generated
            per ``_emit_capsule()`` call and folded into ``agent_input_digest`` /
            ``agent_output_digest`` (and a ``confirmed``-effect's
            ``response_digest``) before hashing — stored as ``digest_salt`` in
            ``compute_attestation`` so the emitting operator can always recompute
            and verify their own capsules. This prevents an outside observer from
            building a rainbow table that correlates low-entropy inputs across
            capsules. Default ``False`` (unsalted, deterministic digests — the
            pre-existing behavior; cross-call digest comparisons keep working
            unchanged). Pass ``True`` for a privacy-sensitive deployment.
        canonicalization_id: The CPB registry identifier naming the algorithm used
            to compute ``capsule_id``.  Written into the top-level
            ``canonicalization_id`` field (the self-describing binding slot —
            inside the signed payload, committed to ``capsule_id``).  Default is
            ``CANONICALIZATION_ID`` (``"jcs"``) — this path always delegates to
            ``agent_action_capsule.emit()``, which builds only ``format_version``
            ``"4"`` and REQUIRES ``canonicalization_id="jcs"`` (§5.1). Pass a
            different registered value only for deliberate negative/mutant
            testing — a mismatched declaration makes the resulting capsule fail
            verification's ``canonicalization_profile_mismatch`` check by design.
        signer: Bring your own :class:`~capsule_emit.signing.Signer` (KMS,
            HSM, TPM, or any object with ``.sign(bytes) -> (signature,
            key_id)``) to sign this capsule with instead of the default
            persisted local keypair. Overrides ``signing_key_path`` when both
            are given.
        signing_key_path: Override where the default
            :class:`~capsule_emit.signing.LocalKeypairSigner` persists its
            key (else ``CAPSULE_SIGNING_KEY_PATH``, else a file next to
            ``ledger``). Ignored when ``signer`` is given.

    Returns:
        :class:`EmitResult` with ``.capsule_id``, ``.anchored``,
        ``.anchor_status``, ``.signature``, and ``.key_id``. Every capsule is
        signed — there is no way to opt out of the self-attested signature
        (see ``capsule_emit.signing``); ``signer=``/``signing_key_path=``
        choose WHICH key signs it, never whether it is signed.
    """
    if human_disposed and approver != "human":
        raise InvariantError(
            "human_disposed=True requires approver='human' — "
            "pass approver='human' or set human_disposed=False"
        )
    if relation is not None and relation != "confirms" and confirms is None:
        raise ValueError(
            f"relation={relation!r} requires confirms=<capsule_id> — "
            "a chain relation needs a chain target"
        )
    # CAPSULE_WITNESS=stub + CAPSULE_ENV=production refuses to run, before
    # anything is written (frozen surface §1a.4) — see
    # capsule_emit.witness.refuse_stub_in_production.
    _witness.refuse_stub_in_production(witness)

    # Per-emit random salt for digest privacy (opt-in — see salt_digests above).
    emit_salt: str | None = secrets.token_hex(16) if salt_digests else None

    compute_att: dict[str, Any] = {}
    _had_digest = False
    if agent_input is not None:
        compute_att["agent_input_digest"] = _digest(agent_input, salt=emit_salt)
        _had_digest = True
    if agent_output is not None:
        compute_att["agent_output_digest"] = _digest(agent_output, salt=emit_salt)
        _had_digest = True
    # Only store the salt when there is at least one digest it was applied to.
    if emit_salt is not None and _had_digest:
        compute_att["digest_salt"] = emit_salt
    if runtime is not None:
        compute_att["runtime"] = runtime
    if extra_compute:
        compute_att.update(extra_compute)

    model_id: str | None = None
    provider: str | None = None
    if model:
        model_id = model.get("model_id")
        provider = model.get("provider")
        extra_chip = {k: v for k, v in model.items() if k not in ("model_id", "provider")}
        if extra_chip:
            compute_att.update(extra_chip)

    effect_record: EffectRecord | None = None
    if effect is not None:
        eff_status = effect.get("status", "dispatched")
        response_digest: str | None = None
        if eff_status == "confirmed":
            # §5.2 confirmed-effect invariant: must supply response_digest.
            # Auto-derive from agent_output when available; else from the
            # confirms capsule_id (the "observed response" in a confirm chain).
            # Salted with the same emit_salt so it matches agent_output_digest
            # (the common case: the same value, digested twice for two fields).
            if agent_output is not None:
                response_digest = _digest(agent_output, salt=emit_salt)
            elif confirms is not None:
                response_digest = _digest({"confirmed_capsule_id": confirms}, salt=emit_salt)
        effect_record = EffectRecord(
            type=effect.get("type", action),
            status=eff_status,
            response_digest=response_digest,
        )

    disposition = Disposition(
        decision=decision,
        approver=approver,
        human_disposed=human_disposed,
        verdict_class=verdict,
        authority=disposition_authority,
    )

    chain_relation: str | None = None
    if confirms is not None:
        chain_relation = relation

    _action_type = action_type if action_type is not None else (
        "decide" if verdict in ("executed", "confirmed", "denied", "blocked", "assessed") else "fyi"
    )
    capsule = _base_emit(
        action_id=None,
        action_type=_action_type,
        operator=operator,
        developer=developer,
        model_id=model_id,
        provider=provider,
        compute_attestation=compute_att if compute_att else None,
        effect=effect_record,
        prior_capsule_id=confirms,
        chain_relation=chain_relation,
        disposition=disposition,
        tool_name=action,
    )

    # Write canonicalization_id into the self-describing binding slot
    # (top-level, inside the signed payload), then compute the PURE,
    # signer-independent capsule_id, THEN sign it -- draft-04 reversal
    # ([capsule-cose-sign1]):
    #
    # 1. capsule_id = compute_capsule_id(capsule) over everything above
    #    (canonicalization_id AND chain committed under "jcs"; capsule_id
    #    excluded as always -- see capsule_emit.canonicalization).
    # 2. capsule["capsule_id"] is set.
    # 3. The producer envelope (a COSE_Sign1 envelope over the raw
    #    capsule_id digest -- the frozen AAC producer-envelope profile) is
    #    built and its hex form + key_id are added as capsule["signature"] /
    #    capsule["key_id"].
    #
    # signature/key_id are added AFTER capsule_id is both computed and set,
    # and are permanently excluded from any capsule_id preimage (never
    # folded in) -- so capsule_id is the same for any signer over identical
    # content ("content-unique, not record-unique"), and no strip-and-
    # recompute dance is needed to verify: capsule_emit.signing
    # .verify_capsule_signature() just recomputes capsule_id (which already
    # excludes them) and checks the envelope against it directly.
    capsule["canonicalization_id"] = canonicalization_id
    capsule["capsule_id"] = compute_capsule_id(capsule)

    signer_obj = _signing.resolve_signer(
        os.fspath(ledger), signer=signer, key_path=signing_key_path
    )
    capsule["signature"], capsule["key_id"] = _signing.sign_producer_envelope(
        signer_obj, capsule["capsule_id"]
    )

    seq = append_to_ledger(capsule, ledger)

    # O16-03: the witness kill switch (``witness=False`` / ``CAPSULE_WITNESS=off``)
    # is the ONE switch that zeroes all egress -- including the legacy anchor
    # channel, even when a caller has explicitly opted it back in via
    # ``anchor=True`` / ``CAPSULE_ANCHOR=legacy-on``. This is what makes the
    # "local-only" posture (frozen surface §1a.3) an honest zero-network
    # guarantee rather than a promise the legacy channel can quietly violate.
    witness_enabled_now = _witness.witness_enabled(witness)
    anchor_enabled = _anchor_enabled(anchor) and witness_enabled_now
    witness_endpoint = witness_url or os.environ.get(_witness.WITNESS_URL_ENV_VAR, None)
    anchor_endpoint = anchor_url or os.environ.get("AAC_ANCHOR_URL", None)

    # Single combined notice, before either default network path is dispatched
    # below — see _print_first_run_disclosure_once's docstring for why this
    # must run first and print at most once per process. Stub witnessing
    # (CAPSULE_WITNESS=stub) is deliberately excluded from "network path
    # active" here — it never dials out, and _witness.maybe_checkpoint()
    # below prints its OWN, stub-specific scream instead (frozen surface
    # §1a.4); this notice must never claim a network attempt that isn't real.
    _print_first_run_disclosure_once(
        anchor_active=anchor_enabled,
        # NOT witness_enabled_now (the O16-03 kill-switch gate, True for
        # both "on" and "stub" -- stub is not the kill switch, and anchor's
        # own decision must still govern when a caller explicitly opts it
        # back in). This notice specifically claims a NETWORK attempt, which
        # stub mode never makes -- see maybe_checkpoint()'s own stub-specific
        # scream instead.
        witness_active=_witness.witness_mode(witness) == "on",
        anchor_endpoint=anchor_endpoint,
        witness_endpoint=witness_endpoint,
    )

    _witness.maybe_checkpoint(
        os.fspath(ledger), ts_url=witness_endpoint, enabled=witness, signer=signer_obj
    )

    capsule_id = capsule["capsule_id"]
    anchored = False
    anchor_status: AnchorStatus
    if not anchor_enabled:
        anchor_status = "skipped"
    else:
        endpoint = anchor_endpoint
        if not _anchor_dependency_available():
            _print_missing_anchor_dependency_notice_once()
        future = async_anchor(capsule_id, ts_url=endpoint)
        _track_pending_anchor(capsule_id, future, endpoint)
        if anchor_wait is None:
            anchor_status = "submitted"
        else:
            result = future.result(timeout=anchor_wait)
            if result is None:
                anchor_status = "submitted"
            else:
                _untrack_pending_anchor(capsule_id)
                if isinstance(result, AnchorResult):
                    anchored = True
                    anchor_status = "confirmed"
                else:
                    anchor_status = "failed"

    return EmitResult(
        capsule_id=capsule_id,
        anchored=anchored,
        capsule=capsule,
        anchor_status=anchor_status,
        signature=capsule["signature"],
        key_id=capsule["key_id"],
        seq=seq,
    )
