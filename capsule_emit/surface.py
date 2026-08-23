# SPDX-License-Identifier: Apache-2.0
"""The seal / carry / compose developer surface — Layer 0.

    from capsule_emit import seal, carry, compose

    capsule = seal(payload)                       # MINT — mine; returns a Capsule
    effect  = carry(receipt_bytes)                # CARRY — theirs, already signed
    action  = compose([auth, guard, act, effect])  # BIND — references members

One authorship axis, three verbs: ``seal`` (I authored the content), ``carry``
(someone else signed it, I bring it in as-transmitted), ``compose`` (this
capsule asserts no new content — it references other capsules). All three
return a :data:`Capsule` and all three append to the log. None of them
re-implements signing or binding — they are thin, opinionated wrappers over
:func:`capsule_emit.core._emit_capsule`, the internal primitive that already
does the CPB-bind + sign + ledger-append work (the removed public ``emit()``
verb wrapped the same primitive). See
``_work/api-verb-naming-design-2026-08-21.md`` §7 for the surface of record
this module implements.

**Vocabulary discipline.** The mint result is a ``capsule`` — never call it a
``receipt``. A *receipt* is what a witness/transparency-service returns about
a capsule you already sealed; ``seal()``/``carry()``/``compose()`` never
return one.

**Import discipline.** The pinned import style is
``from capsule_emit import seal, carry, compose``. Never
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
mechanism for binding a member that lives in someone else's log: ``carry()``
localizes the foreign artifact as a capsule in *your* log first, and then
``compose()`` references it exactly like any member you authored yourself.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .core import EmitResult, _emit_capsule

__all__ = ["Capsule", "seal", "carry", "compose"]

#: The noun. seal()/carry()/compose() all return this type.
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
            f"{who} must be a Capsule returned by seal(), carry(), or compose() "
            f"— got {type(value).__name__}. compose() binds capsules that are "
            "already appended to the log; it never guesses membership."
        )
    return value


def seal(payload: Any, *, action: str = "seal", **kwargs: Any) -> Capsule:
    """Mint a capsule you authored. The canonical line: ``capsule = seal(payload)``.

    Wraps the internal ``_emit_capsule`` primitive — *payload* is sealed as
    ``agent_input`` (digest-committed; the raw value never leaves the
    process). Any keyword the primitive accepts (``operator``,
    ``developer``, ``model``, ``effect``, ...) may be passed through.
    """
    return _emit_capsule(action, agent_input=payload, **kwargs)


def carry(receipt_bytes: bytes | bytearray | str, *, action: str = "carry", **kwargs: Any) -> Capsule:
    """Bring in a foreign, already-signed artifact, bound as-transmitted.

    *receipt_bytes* is someone else's already-signed record — it is committed
    by the SHA-256 digest of its exact transmitted bytes (no JCS
    re-canonicalization; that would silently reinterpret bytes you did not
    sign). This localizes the foreign artifact as a capsule in your own log,
    which is what lets ``compose()`` reference it exactly like any member you
    authored yourself (carry-then-compose).
    """
    raw = receipt_bytes.encode("utf-8") if isinstance(receipt_bytes, str) else bytes(receipt_bytes)
    carried_ref = {
        "type": _CARRIED_TYPE,
        "digest_alg": _DIGEST_ALG,
        "digest": hashlib.sha256(raw).hexdigest(),
    }
    extra_compute = dict(kwargs.pop("extra_compute", None) or {})
    extra_compute["carried_artifact"] = carried_ref
    return _emit_capsule(action, extra_compute=extra_compute, **kwargs)


def compose(members: Iterable[Capsule], *, action: str = "compose", **kwargs: Any) -> Capsule:
    """Bind existing capsules into one composition capsule — references, asserts nothing new.

    Every member must already be a :data:`Capsule` returned by ``seal()``,
    ``carry()``, or ``compose()`` — and therefore already appended to the
    log. A member that is not (a raw dict, a payload, anything else) is a
    caller error and raises :class:`TypeError` rather than being guessed at.

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
