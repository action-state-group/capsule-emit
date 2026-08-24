# SPDX-License-Identifier: Apache-2.0
"""The seal / carry / compose / received developer surface — Layer 0.

    from capsule_emit import seal, compose, received

    capsule = seal(payload)                        # MINT — mine; returns a Capsule
    effect  = received(their_bytes, type="...")    # CARRY — theirs, already signed,
                                                     #   under their declared type
    capsule = seal(received(their_bytes, type="...")) # same carry, nested one
                                                     #   slot wrapper deep (§ below)
    action  = compose([auth, guard, act, effect])   # BIND — references members

One authorship axis, four verbs: ``seal`` (I authored the content),
``received`` (someone else signed it, I bring it in as-transmitted, under
their declared type — see the dispatch rule below), ``compose`` (this
capsule asserts no new content — it references other capsules), and
``carry`` (kept, unchanged, alongside ``received()`` for this release — see
"Migration from carry()" below). All four return a :data:`Capsule` and all
append to the log. None of them re-implements signing or binding — they are
thin, opinionated wrappers over :func:`capsule_emit.core._emit_capsule`, the
internal primitive that already does the CPB-bind + sign + ledger-append
work (the removed public ``emit()`` verb wrapped the same primitive). See
``_work/dev-surface-v4-2026-08-24.md`` §1 for the frozen surface of record
this module implements.

**Dispatch rule for foreign bytes, stated once.** ``received()`` is legal
**standalone** (``effect = received(bytes, type=...)`` — a carry; their
record enters your log as-is, and its slot position is assigned later, when
a composition cites it) **or nested inside** ``seal()``
(``seal(received(bytes, type=...))``) — the two forms produce the identical
capsule: ``seal()`` recognizes an already-carried :data:`Capsule` and returns
it unchanged rather than re-sealing it. Bare, undeclared bytes passed
straight to ``seal()`` are always refused — ambiguity between "content I
authored" and "bytes someone else signed" is never guessed; the error names
``received()``.

**Migration from carry().** ``carry()`` still works, unchanged, in this
release — ``received()`` ships alongside it rather than replacing it in the
same change, so the new-verb risk is decoupled from a breaking removal.
``received()`` is the more expressive form: it records the foreign artifact
under its own declared registered type (``type="machine-mandate"``, say)
instead of ``carry()``'s generic ``"foreign-artifact"`` marker.
``carry()``'s deprecation is a later, separate change.

**Vocabulary discipline.** The mint result is a ``capsule`` — never call it a
``receipt``. A *receipt* is what a witness/transparency-service returns about
a capsule you already sealed; ``seal()``/``carry()``/``received()``/
``compose()`` never return one.

**Import discipline.** The pinned import style is
``from capsule_emit import seal, carry, received, compose``. Never
``import capsule_emit as capsule`` — that shadows the noun (the ``capsule``
variable the canonical line assigns to). ``capsule_emit`` deliberately exports
no symbol named ``capsule`` so this mistake fails fast on first use rather
than silently binding the module where a Capsule was expected.

**Layer 0 only.** This module does not import ``capsule_emit.checkpoint``
(the opt-in Checkpointed Local Log / witness layer) and must keep working
with nothing configured. Consequently ``compose()`` here binds members by
their CPB typed digest reference alone (``{type, digest_alg, digest}``) — the
``{log_id, leaf_index, inclusion_proof}`` upgrade is the CLL layer's, additive
and never required to use this surface (design doc §7a: the digest reference
is the member's identity; log coordinates are an upgrade, not a second
vocabulary).

**Cross-stream composition = carry-then-compose.** There is no separate
mechanism for binding a member that lives in someone else's log:
``received()`` (or, unchanged, ``carry()``) localizes the foreign artifact as
a capsule in *your* log first, and then ``compose()`` references it exactly
like any member you authored yourself.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .core import EmitResult, _emit_capsule

__all__ = ["Capsule", "seal", "carry", "received", "compose"]

#: The noun. seal()/carry()/received()/compose() all return this type.
#: An alias, not a new class — Capsule *is* an EmitResult; the rename is a
#: vocabulary fix (spec §6: the constructor returns a capsule, never a
#: receipt), not a new shape.
Capsule = EmitResult

_DIGEST_ALG = "SHA-256"
_MEMBER_TYPE = "capsule"
_CARRIED_TYPE = "foreign-artifact"


def _require_capsule(value: Any, *, who: str) -> Capsule:
    if not isinstance(value, EmitResult):
        raise TypeError(
            f"{who} must be a Capsule returned by seal(), carry(), received(), or "
            f"compose() — got {type(value).__name__}. compose() binds capsules "
            "that are already appended to the log; it never guesses membership."
        )
    return value


def _carried_artifact_ref(capsule: Capsule) -> dict[str, Any] | None:
    return (
        capsule.capsule.get("model_attestation", {})
        .get("compute_attestation", {})
        .get("carried_artifact")
    )


def seal(payload: Any, *, action: str = "seal", **kwargs: Any) -> Capsule:
    """Mint a capsule you authored. The canonical line: ``capsule = seal(payload)``.

    Wraps the internal ``_emit_capsule`` primitive — *payload* is sealed as
    ``agent_input`` (digest-committed; the raw value never leaves the
    process). Any keyword the primitive accepts (``operator``,
    ``developer``, ``model``, ``effect``, ...) may be passed through.

    **Dispatch is never guessed.** Raw ``bytes``/``bytearray`` are always
    refused — undeclared foreign bytes are ambiguous (content you authored,
    or someone else's signed artifact?) and the error names the fix:
    ``seal(received(bytes, type=...))``. A :data:`Capsule` already produced
    by ``received()``/``carry()`` (detected by its ``carried_artifact``
    field) is passed through unchanged rather than re-sealed — this is the
    nested-in-``seal()`` dispatch form, and it is byte-identical to calling
    ``received()`` standalone.
    """
    if isinstance(payload, (bytes, bytearray)):
        raise TypeError(
            "seal() does not accept raw bytes as payload — undeclared foreign "
            "bytes are always ambiguous (content you authored, or someone "
            "else's already-signed artifact?). Wrap already-signed foreign "
            "bytes in received(bytes, type=...) instead: "
            "seal(received(bytes, type=...))."
        )
    if isinstance(payload, EmitResult) and _carried_artifact_ref(payload) is not None:
        return payload
    return _emit_capsule(action, agent_input=payload, **kwargs)


def _carry(
    artifact_bytes: bytes | bytearray | str, *, carried_type: str, action: str, kwargs: dict[str, Any]
) -> Capsule:
    """Shared carry mechanism behind ``carry()`` and ``received()``.

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
    """
    raw = artifact_bytes.encode("utf-8") if isinstance(artifact_bytes, str) else bytes(artifact_bytes)
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


def carry(receipt_bytes: bytes | bytearray | str, *, action: str = "carry", **kwargs: Any) -> Capsule:
    """Bring in a foreign, already-signed artifact, bound as-transmitted.

    *receipt_bytes* is someone else's already-signed record — it is committed
    by the SHA-256 digest of its exact transmitted bytes (no JCS
    re-canonicalization; that would silently reinterpret bytes you did not
    sign). This localizes the foreign artifact as a capsule in your own log,
    which is what lets ``compose()`` reference it exactly like any member you
    authored yourself (carry-then-compose).

    Kept, unchanged, alongside :func:`received` for this release — see the
    module docstring's "Migration from carry()". The foreign artifact is
    recorded under the generic ``"foreign-artifact"`` CPB type; ``received()``
    records it under its own declared registered type instead.
    """
    return _carry(receipt_bytes, carried_type=_CARRIED_TYPE, action=action, kwargs=kwargs)


def received(artifact_bytes: bytes | bytearray | str, *, type: str, action: str = "carry", **kwargs: Any) -> Capsule:
    """Bring in a foreign, already-signed artifact under its own declared type.

    *artifact_bytes* is someone else's already-signed record — committed by
    the SHA-256 digest of its exact transmitted bytes (no JCS
    re-canonicalization; that would silently reinterpret bytes you did not
    sign). *type* is the artifact's own registered CPB type (e.g.
    ``"machine-mandate"``) — declared by the caller, never guessed, and never
    re-signed.

    **Two dispatch forms, one result.** Called directly —
    ``effect = received(bytes, type=...)`` — this performs the carry now and
    returns the resulting :data:`Capsule`: the standalone form; its slot
    position is assigned later, when a composition cites it. Nested one slot
    wrapper deep — ``capsule = seal(received(bytes, type=...))`` — produces
    the identical capsule: :func:`seal` recognizes an already-carried
    :data:`Capsule` and returns it unchanged rather than re-sealing it. Bare,
    undeclared bytes passed straight to :func:`seal` are always refused —
    ambiguity between "content I authored" and "bytes someone else signed"
    is never guessed.
    """
    return _carry(artifact_bytes, carried_type=type, action=action, kwargs=kwargs)


def compose(members: Iterable[Capsule], *, action: str = "compose", **kwargs: Any) -> Capsule:
    """Bind existing capsules into one composition capsule — references, asserts nothing new.

    Every member must already be a :data:`Capsule` returned by ``seal()``,
    ``carry()``, ``received()``, or ``compose()`` — and therefore already
    appended to the log. A member that is not (a raw dict, a payload,
    anything else) is a caller error and raises :class:`TypeError` rather
    than being guessed at.

    Layer 0 references members by CPB typed digest ref alone
    (``{type: "capsule", digest_alg: "SHA-256", digest: capsule_id}``); the
    ``{log_id, leaf_index, inclusion_proof}`` upgrade is the CLL/checkpoint
    layer's and is never required here.
    """
    member_list = [_require_capsule(m, who="compose() member") for m in members]
    if not member_list:
        raise ValueError("compose() requires at least one member")
    refs = [
        {"type": _MEMBER_TYPE, "digest_alg": _DIGEST_ALG, "digest": m.capsule_id}
        for m in member_list
    ]
    extra_compute = dict(kwargs.pop("extra_compute", None) or {})
    extra_compute["composed_members"] = refs
    return _emit_capsule(action, extra_compute=extra_compute, **kwargs)
