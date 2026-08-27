# SPDX-License-Identifier: Apache-2.0
"""The seal / received / who / can / did / audit developer surface — Layer 0.

    from capsule_emit import seal, received, who, can, did, audit

    capsule = seal(payload)                        # MINT — mine; returns a Capsule
    effect  = received(their_bytes, type="...")    # CARRY — theirs, already signed,
                                                     #   under their declared type
    capsule = seal(received(their_bytes, type="...")) # same carry, nested one
                                                     #   slot wrapper deep (§ below)
    capsule = seal(
        who(delegation_record),                     # BIND — one composition
        can(received(mandate_jws, type="...")),      #   capsule referencing
        did(payment_action),                         #   its slot members
    )

One authorship axis: ``seal`` (I authored the content — one payload, or a
declared set of slot members) and ``received`` (someone else signed it, I
bring it in as-transmitted, under their declared type — see the dispatch
rule below). Both return a :data:`Capsule` and both append to the log. Slot
membership is always declared, never inferred — see ``who``/``can``/``did``/
``audit`` below. Neither verb re-implements signing or binding — they are
thin, opinionated wrappers over :func:`capsule_emit.core._emit_capsule`, the
internal primitive that already does the CPB-bind + sign + ledger-append
work. See ``_work/dev-surface-v4-2026-08-24.md`` §1/§3 for the frozen
surface of record this module implements.

**Dispatch rule for foreign bytes, stated once.** ``received()`` is legal
**standalone** (``effect = received(bytes, type=...)`` — a carry; their
record enters your log as-is, and its slot position is assigned later, when
a composition cites it) **or nested inside** ``seal()`` — directly
(``seal(received(bytes, type=...))``) or inside any slot wrapper
(``can(received(bytes, type=...))``) — every one of these produces the
identical carried capsule: ``seal()``/the slot wrappers recognize an
already-carried :data:`Capsule` and reference or return it unchanged rather
than re-sealing it. Bare, undeclared bytes passed straight to ``seal()`` or
to any slot wrapper are always refused — ambiguity between "content I
authored" and "bytes someone else signed" is never guessed; the error names
``received()``.

**Slots — declared membership, never inferred.** ``who()``/``can()``/
``did()``/``audit()`` mark a payload's (or an already-sealed/carried
Capsule's) role in one action's account — the composition I-D's four-leg
model (WHO/CAN/WHAT/AUDIT). Passing two or more slot wrappers into
``seal()`` mints (or references) each member, then mints ONE composition
capsule that cites every member by CPB typed digest ref, annotated with its
slot — composition semantics (member slot annotations), never a new
protected claim, and the single-payload ``seal(payload)`` form is
byte-for-byte unchanged by any of this. A slot wrapper's value is either a
payload you author here (minted into its own capsule, under that slot's
name as its action) or a :data:`Capsule` you already hold (referenced as-is
— e.g. ``can(received(...))``, or any capsule from ``seal()``/``received()``
you already produced). The verb never infers which capsules belong together
— only what is explicitly wrapped in a slot is ever a member.

**Vocabulary discipline.** The mint result is a ``capsule`` — never call it a
``receipt``. A *receipt* is what a witness/transparency-service returns about
a capsule you already sealed; ``seal()``/``received()`` never return one.

**Import discipline.** The pinned import style is
``from capsule_emit import seal, received, who, can, did, audit``. Never
``import capsule_emit as capsule`` — that shadows the noun (the ``capsule``
variable the canonical line assigns to). ``capsule_emit`` deliberately exports
no symbol named ``capsule`` so this mistake fails fast on first use rather
than silently binding the module where a Capsule was expected.

**Layer 0 only.** This module does not import ``capsule_emit.checkpoint``
(the opt-in Checkpointed Local Log / witness layer) and must keep working
with nothing configured. Consequently a composition here binds members by
their CPB typed digest reference alone (``{type, digest_alg, digest}``,
``slot`` added when the member has one) — the ``{log_id, leaf_index,
inclusion_proof}`` upgrade is the CLL layer's, additive and never required to
use this surface (design doc §7a: the digest reference is the member's
identity; log coordinates are an upgrade, not a second vocabulary).

**Cross-stream composition = carry-then-compose.** There is no separate
mechanism for binding a member that lives in someone else's log:
``received()`` localizes the foreign artifact as a capsule in *your* log
first, and then a slot wrapper references it exactly like any member you
authored yourself.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .core import _DEFAULT_LEDGER, EmitResult, _emit_capsule

__all__ = ["Capsule", "seal", "received", "who", "can", "did", "audit", "push"]

#: The noun. seal()/received() (standalone or composed) all return this type.
#: An alias, not a new class — Capsule *is* an EmitResult; the rename is a
#: vocabulary fix (spec §6: the constructor returns a capsule, never a
#: receipt), not a new shape.
Capsule = EmitResult

_DIGEST_ALG = "SHA-256"
_MEMBER_TYPE = "capsule"
_CARRIED_TYPE = "foreign-artifact"

#: The four slot names, in the composition I-D's canonical order — the
#: WHO/CAN/WHAT/AUDIT four-leg model (frozen surface §3). ``did()`` fills
#: the WHAT leg (the slot name reads as "what did you do").
_SLOTS = ("who", "can", "did", "audit")


def _require_capsule(value: Any, *, who: str) -> Capsule:
    if not isinstance(value, EmitResult):
        raise TypeError(
            f"{who} must be a Capsule returned by seal() or received() — got "
            f"{type(value).__name__}. Composition binds capsules that are "
            "already appended to the log; it never guesses membership."
        )
    return value


def _carried_artifact_ref(capsule: Capsule) -> dict[str, Any] | None:
    return (
        capsule.capsule.get("model_attestation", {})
        .get("compute_attestation", {})
        .get("carried_artifact")
    )


class _SlotMember:
    """A payload (or already-sealed/carried :data:`Capsule`) tagged with its
    slot role — the object ``who()``/``can()``/``did()``/``audit()`` return.

    Not part of the public surface directly: these are only ever passed as
    positional arguments into :func:`seal`, which resolves each one (minting
    a fresh capsule for a raw payload, or referencing an already-produced
    :data:`Capsule` unchanged) and binds the results into one composition.
    """

    __slots__ = ("slot", "value")

    def __init__(self, slot: str, value: Any) -> None:
        self.slot = slot
        self.value = value

    def __repr__(self) -> str:
        return f"{self.slot}({self.value!r})"


def _slot_wrapper(slot: str, value: Any) -> _SlotMember:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"{slot}() does not accept raw bytes — undeclared foreign bytes "
            "are always ambiguous (content you authored, or someone else's "
            "already-signed artifact?). Wrap already-signed foreign bytes in "
            f"received(bytes, type=...) instead: {slot}(received(bytes, type=...))."
        )
    return _SlotMember(slot, value)


def who(value: Any) -> _SlotMember:
    """Mark *value* as this action's WHO-slot member — identity/pedigree
    evidence (a delegation record, a key-binding receipt, ...). See the
    module docstring's "Slots" section; passed into :func:`seal`."""
    return _slot_wrapper("who", value)


def can(value: Any) -> _SlotMember:
    """Mark *value* as this action's CAN-slot member — the authority/mandate
    that permitted it (often ``received(mandate_jws, type=...)``, someone
    else's already-signed grant). See the module docstring's "Slots"
    section; passed into :func:`seal`."""
    return _slot_wrapper("can", value)


def did(value: Any) -> _SlotMember:
    """Mark *value* as this action's WHAT-slot member — the action itself.
    See the module docstring's "Slots" section; passed into :func:`seal`."""
    return _slot_wrapper("did", value)


def audit(value: Any) -> _SlotMember:
    """Mark *value* as this action's AUDIT-slot member — whoever/whatever
    checked (a TEE attestation, an operator countersign, ...). Reserved by
    the frozen surface for future evidence kinds; see the module docstring's
    "Slots" section. Passed into :func:`seal`."""
    return _slot_wrapper("audit", value)


#: Sentinel distinguishing "caller did not pass action" from "caller passed
#: action='seal' explicitly" — seal()'s carried-Capsule pass-through path
#: (below) must reject an explicit action just as it rejects any other outer
#: option, so the default cannot be the literal string "seal".
_ACTION_UNSET = object()


def seal(*args: Any, action: Any = _ACTION_UNSET, **kwargs: Any) -> Capsule:
    """Mint a capsule you authored. The canonical line: ``capsule = seal(payload)``.

    Wraps the internal ``_emit_capsule`` primitive — *payload* is sealed as
    ``agent_input`` (digest-committed; the raw value never leaves the
    process). Any keyword the primitive accepts (``operator``,
    ``developer``, ``model``, ``effect``, ...) may be passed through.

    **Dispatch is never guessed.** Raw ``bytes``/``bytearray``/``memoryview``
    are always refused — undeclared foreign bytes are ambiguous (content you
    authored, or someone else's signed artifact?) and the error names the
    fix: ``seal(received(bytes, type=...))``. A :data:`Capsule` already
    produced by ``received()`` (detected by its ``carried_artifact`` field)
    is passed through unchanged rather than re-sealed — this is the
    nested-in-``seal()`` dispatch form, and it is byte-identical to calling
    ``received()`` standalone.

    **Outer options on an already-carried Capsule are rejected, not
    dropped.** ``received()`` already ran ``_emit_capsule`` (and so already
    fixed the ledger, signer, and witness disposition) by the time ``seal()``
    sees the result — any ``ledger=``/``signing_key_path=``/``witness=``/
    ``action=``/other keyword passed to this outer ``seal()`` call would be
    silently thrown away, which for ``witness=False`` specifically means
    witnessing could fire despite an explicit opt-out. Pass those options to
    ``received()`` itself instead.

    **Two or more slot wrappers compose.** ``seal(who(a), can(b), did(c))``
    mints (or references) each member and returns ONE composition capsule
    citing them by slot — see the module docstring's "Slots" section and
    :func:`who`/:func:`can`/:func:`did`/:func:`audit`. Mixing a plain
    payload with slot wrappers in one call is refused: wrap every member,
    including a single one, in its own slot.
    """
    if not args:
        raise TypeError(
            "seal() requires a payload (seal(payload)) or one or more slot "
            "wrappers (seal(who(...), can(...), did(...), audit(...)))"
        )
    if any(isinstance(a, _SlotMember) for a in args):
        if not all(isinstance(a, _SlotMember) for a in args):
            raise TypeError(
                "seal() cannot mix a plain payload with slot wrappers in one "
                "call — wrap every member in a slot: "
                "seal(who(...), can(...), did(...), audit(...))"
            )
        if action is not _ACTION_UNSET:
            raise TypeError(
                "seal()'s slot-form does not accept action= — the composition's "
                "own action is fixed ('compose'); a member that needs its own "
                "action should be sealed explicitly before being wrapped, e.g. "
                "did(seal(payload, action=...))."
            )
        return _seal_slots(list(args), **kwargs)
    if len(args) > 1:
        raise TypeError(
            "seal() takes exactly one payload — to bind more than one member "
            "into one capsule, use slot wrappers: "
            "seal(who(...), can(...), did(...), audit(...))"
        )
    payload = args[0]
    if isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(
            "seal() does not accept raw bytes as payload — undeclared foreign "
            "bytes are always ambiguous (content you authored, or someone "
            "else's already-signed artifact?). Wrap already-signed foreign "
            "bytes in received(bytes, type=...) instead: "
            "seal(received(bytes, type=...))."
        )
    if isinstance(payload, EmitResult) and _carried_artifact_ref(payload) is not None:
        offending = sorted(kwargs) + (["action"] if action is not _ACTION_UNSET else [])
        if offending:
            raise TypeError(
                "seal() was called on an already-carried Capsule (from "
                f"received()) together with outer option(s) {offending} "
                "— seal() only passes such a Capsule through unchanged, so "
                "these options would be silently dropped, and witness=False "
                "specifically could be silently ignored, arming witnessing "
                "despite an explicit opt-out. Pass ledger / signing_key_path / "
                "witness / action / etc. to received() itself instead: "
                "received(bytes, type=..., ledger=..., witness=False)."
            )
        return payload
    resolved_action = "seal" if action is _ACTION_UNSET else action
    return _emit_capsule(resolved_action, agent_input=payload, **kwargs)


def _resolve_slot_member(member: _SlotMember, **kwargs: Any) -> Capsule:
    """Resolve one slot wrapper's value to a Capsule: reference an
    already-sealed/carried one unchanged (never re-minted — this is what
    makes ``can(received(bytes, type=...))`` byte-identical to calling
    ``received(bytes, type=...)`` standalone, O8), or mint a raw payload as
    its own capsule under its slot name as the action."""
    value = member.value
    if isinstance(value, EmitResult):
        return value
    return _emit_capsule(member.slot, agent_input=value, **kwargs)


def _seal_slots(members: list[_SlotMember], **kwargs: Any) -> Capsule:
    resolved: list[Capsule] = []
    slots: dict[str, str] = {}
    for member in members:
        capsule = _resolve_slot_member(member, **kwargs)
        resolved.append(capsule)
        slots[capsule.capsule_id] = member.slot
    return _compose(resolved, slots=slots, **kwargs)


def _carry(
    artifact_bytes: bytes | bytearray | memoryview | str,
    *,
    carried_type: str,
    action: str,
    kwargs: dict[str, Any],
) -> Capsule:
    """Shared carry mechanism behind ``received()``.

    **Two addresses, two facts.** This capsule's own ``capsule_id`` commits to
    the carried bytes as ITS payload (``carried_input_digest``, in the same
    payload-commitment slot ``agent_input_digest``/``agent_output_digest``
    occupy for ``seal()``) while ``carried_artifact.digest`` keeps identifying
    the foreign record unchanged — theirs identifies their record, yours
    identifies your act of holding it. ``carried_input_digest`` is not named
    ``agent_input_digest`` because that field's contract is
    ``SHA-256(JCS(agent_input))`` over a JSON-native value; a carried artifact
    is opaque bytes that must never be JCS-reinterpreted, so it gets its own,
    equally-raw digest field instead.

    **No implicit buffer coercion.** ``bytes(value)`` accepts far more than
    "an explicit buffer" — ``bytes(7)`` NUL-pads to 7 bytes, ``bytes(True)``
    becomes a single ``\\x01`` byte, ``bytes([1, 2, 3])`` treats a list of
    ints as a byte sequence — and every one of those silently commits the
    capsule to bytes the caller never actually transmitted. Only ``str`` (utf-8
    encoded) and explicit buffer types (``bytes``/``bytearray``/``memoryview``)
    are accepted; anything else is a caller error, raised rather than guessed.
    """
    if isinstance(artifact_bytes, str):
        raw = artifact_bytes.encode("utf-8")
    elif isinstance(artifact_bytes, (bytes, bytearray, memoryview)):
        raw = bytes(artifact_bytes)
    else:
        raise TypeError(
            "received() artifact_bytes must be str (utf-8 encoded) or "
            "an explicit buffer (bytes/bytearray/memoryview) — got "
            f"{type(artifact_bytes).__name__}. Implicit coercions like "
            "bytes(some_int) or bytes(list_of_ints) would commit the capsule "
            "to bytes the caller never actually transmitted, so they are "
            "refused rather than guessed at."
        )
    carried_digest = hashlib.sha256(raw).hexdigest()
    carried_ref = {
        "type": carried_type,
        "digest_alg": _DIGEST_ALG,
        "digest": carried_digest,
    }
    extra_compute = dict(kwargs.pop("extra_compute", None) or {})
    extra_compute["carried_artifact"] = carried_ref
    extra_compute["carried_input_digest"] = carried_digest
    return _emit_capsule(action, extra_compute=extra_compute, **kwargs)


def received(
    artifact_bytes: bytes | bytearray | memoryview | str, *, type: str, action: str = "carry", **kwargs: Any
) -> Capsule:
    """Bring in a foreign, already-signed artifact under its own declared type.

    *artifact_bytes* is someone else's already-signed record — committed by
    the SHA-256 digest of its exact transmitted bytes (no JCS
    re-canonicalization; that would silently reinterpret bytes you did not
    sign). *type* is the artifact's own registered CPB type (e.g.
    ``"machine-mandate"``) — declared by the caller, never guessed, and never
    re-signed. Must be a non-empty string; a null/empty/non-string *type*
    would mint a signed capsule with a null, absent, or wrong committed type
    after jcs-normalization, so it is rejected rather than minted.

    **Two dispatch forms, one result.** Called directly —
    ``effect = received(bytes, type=...)`` — this performs the carry now and
    returns the resulting :data:`Capsule`: the standalone form; its slot
    position is assigned later, when a composition cites it. Nested one slot
    wrapper deep — ``capsule = seal(received(bytes, type=...))`` or inside any
    slot wrapper (``can(received(bytes, type=...))``) — produces the
    identical capsule: :func:`seal` and every slot wrapper recognize an
    already-carried :data:`Capsule` and reference or return it unchanged
    rather than re-sealing it. Bare, undeclared bytes passed straight to
    :func:`seal` or to a slot wrapper are always refused — ambiguity between
    "content I authored" and "bytes someone else signed" is never guessed.
    """
    if not isinstance(type, str) or not type.strip():
        raise TypeError(
            f"received() requires type to be a non-empty string naming the "
            f"artifact's own registered CPB type (e.g. type=\"machine-mandate\") "
            f"— got {type!r}. A null/empty/non-string type would mint a signed "
            "capsule with a null, absent, or wrong committed type."
        )
    return _carry(artifact_bytes, carried_type=type, action=action, kwargs=kwargs)


def _compose(
    members: Iterable[Capsule],
    *,
    action: str = "compose",
    slots: dict[str, str] | None = None,
    **kwargs: Any,
) -> Capsule:
    """Private: bind existing capsules into one composition capsule —
    references, asserts nothing new. The flat-bind logic behind the public
    v3 ``compose()`` verb (removed from the public surface, clean break —
    frozen surface §1/§9); the slot-form (``seal(who(...), can(...), ...)``,
    see :func:`_seal_slots`) is now the only public entry point that reaches
    this.

    Every member must already be a :data:`Capsule` returned by ``seal()`` or
    ``received()`` — and therefore already appended to the log. A member
    that is not (a raw dict, a payload, anything else) is a caller error and
    raises :class:`TypeError` rather than being guessed at.

    *slots*, when given, maps a member's ``capsule_id`` to its slot name
    (``"who"``/``"can"``/``"did"``/``"audit"``) — recorded on that member's
    ref as composition semantics (an extra ``slot`` key alongside the CPB
    typed digest ref), never a new protected claim on the member's own
    capsule and never a change to #107's capsule_id construction.

    Layer 0 references members by CPB typed digest ref alone
    (``{type: "capsule", digest_alg: "SHA-256", digest: capsule_id}``); the
    ``{log_id, leaf_index, inclusion_proof}`` upgrade is the CLL/checkpoint
    layer's and is never required here.
    """
    member_list = [_require_capsule(m, who="composition member") for m in members]
    if not member_list:
        raise ValueError("composition requires at least one member")
    refs = []
    for m in member_list:
        ref = {"type": _MEMBER_TYPE, "digest_alg": _DIGEST_ALG, "digest": m.capsule_id}
        if slots is not None and m.capsule_id in slots:
            ref["slot"] = slots[m.capsule_id]
        refs.append(ref)
    extra_compute = dict(kwargs.pop("extra_compute", None) or {})
    extra_compute["composed_members"] = refs
    return _emit_capsule(action, extra_compute=extra_compute, **kwargs)


def push(
    ledger: Any = _DEFAULT_LEDGER,
    *,
    ts_url: str | list[str] | None = None,
    witness: bool | None = None,
) -> Any:
    """Force a checkpoint now — frozen surface §1's "one verb for urgency".

    A thin call into the existing checkpoint layer (``capsule_emit.witness``)
    — see that module's ``push()`` for the full contract (synchronous,
    cadence-independent, no-op when there is nothing new to checkpoint or
    when witnessing is off). Re-exported here so the write-verb family
    (``seal``, ``push``) lives on one import line, per the frozen surface's
    §5 verb table.
    """
    import os

    from . import witness as _witness

    return _witness.push(os.fspath(ledger), ts_url=ts_url, witness=witness)
