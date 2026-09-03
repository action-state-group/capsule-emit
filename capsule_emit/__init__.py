# SPDX-License-Identifier: Apache-2.0
"""capsule-emit — the seal / received / who / can / did / audit surface for Agent Action Capsules.

The developer surface, on one authorship axis — never
``import capsule_emit as capsule`` (it would shadow the noun the canonical
line below assigns to):

    from capsule_emit import seal, received, who, can, did, audit

    capsule = seal(payload)                          # MINT — mine
    effect  = received(their_bytes, type="...")      # CARRY — theirs, already
                                                       #   signed, declared type
    capsule = seal(
        who(delegation_record), can(effect), did(action)
    )                                                 # BIND — one composition,
                                                       #   members by slot

All return a :class:`Capsule` (never call it a ``receipt`` — that word is
reserved for what a witness/transparency-service returns) and all append to
the log. See ``capsule_emit.surface`` for the full contract, including the
standalone vs nested-in-``seal()``/nested-in-a-slot dispatch rule for
foreign bytes, and the slot-composition semantics of ``who``/``can``/
``did``/``audit``.

``push()`` forces an immediate checkpoint (the write family's second verb —
"one verb for urgency", frozen surface §1) instead of waiting on cadence.

**Clean break (2026-08-22):** ``emit()`` was renamed. It remains importable
for one release as a raising stub — ``from capsule_emit import emit`` still
works, but calling it raises ``RuntimeError`` pointing at ``seal()``/
``received()`` — and will be removed entirely in a future release.

**Clean break (2026-08-27):** ``compose()`` and ``carry()`` — the v3 flat-bind
verbs — are removed from the public surface (frozen surface §1/§9's slot-form
supersedes them; no deprecation period, there were no users yet).
``compose()``'s flat-bind body survives internally as the private helper the
slot-form calls; ``carry()``'s body was already ``received()``'s.

Witnessing is on by default (the CLL checkpoint/witness stream — async,
checkpoint-only (never capsule content), lazy per ledger — see
``capsule_emit.witness`` and ``docs/checkpoint.md``). The older per-capsule
anchor channel is a legacy,
non-default opt-in (``anchor=True`` / ``CAPSULE_ANCHOR=legacy-on``) kept only
as a rollback path. Every capsule is also cryptographically signed, always,
by a persisted producer key (see ``capsule_emit.signing``) — this one has no
off switch, only a choice of *which* key signs (``signer=``/
``signing_key_path=``). Ledger is written to ``ledger.jsonl`` by default. All
of the above are configurable.
"""
from typing import Any, NoReturn

from .adjudication import contradicted, seal_adjudication
from .approval import list_pending, seal_approval
from .core import EmitResult
from .disclosure import DisclosureError, build_disclosure_envelope
from .gate import (
    CheckResult,
    Constraint,
    EscalationCallback,
    GateBlockedError,
    GateResult,
    gate_and_emit,
    run_gate,
)
from .holds import Action as HoldAction
from .holds import HoldDecision, HoldEngine, HoldStatus
from .ledger import LedgerLockedError, append_to_ledger, read_ledger
from .ledger import show as ledger_show
from .ledger import view as ledger_view
from .ledger import view_chains as ledger_view_chains
from .manifest import ManifestDeclaration, find_manifest, load_manifest
from .numbers import CANONICALIZATION_ID, float_to_str
from .signing import (
    LocalKeypairSigner,
    RotationRecord,
    Signer,
    verify_capsule_signature,
    verify_store_signed,
)
from .surface import Capsule, audit, can, did, push, received, seal, who
from .verify import InputDigestResult, VerifyReason, verify_input_digest
from .verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationResult,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)

__version__ = "0.7.0"


def emit(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Removed. ``emit()`` was renamed — use ``seal()`` or ``received()``.

    This raising stub exists for one release so callers get a clear error
    instead of an ``ImportError``; ``emit()`` itself will be removed after
    that. See ``capsule_emit.surface`` for the replacement verbs.
    """
    raise RuntimeError(
        "emit() was renamed; use seal() or received() instead — see "
        "capsule_emit.surface. emit() will be removed in a future release."
    )


__all__ = [
    "__version__",
    # seal / received / who / can / did / audit / push — the developer surface (Layer 0)
    "Capsule",
    "seal",
    "received",
    "who",
    "can",
    "did",
    "audit",
    "push",
    # Core
    "emit",
    "EmitResult",
    # Approval record + pending
    "seal_approval",
    "list_pending",
    # Adjudication record (twin comparison)
    "seal_adjudication",
    "contradicted",
    # Gate / wicket
    "Constraint",
    "CheckResult",
    "GateResult",
    "GateBlockedError",
    "EscalationCallback",
    "run_gate",
    "gate_and_emit",
    # Holds (reservation-as-capsule)
    "HoldAction",
    "HoldDecision",
    "HoldEngine",
    "HoldStatus",
    # Ledger
    "append_to_ledger",
    "LedgerLockedError",
    "read_ledger",
    "ledger_view",
    "ledger_view_chains",
    "ledger_show",
    # Manifest
    "load_manifest",
    "find_manifest",
    "ManifestDeclaration",
    # Number conversion (RFC 8785 §3.2.2.3) and canonicalization identifier
    "CANONICALIZATION_ID",
    "float_to_str",
    "KNOWN_ALGORITHMS",
    "verify_canonicalization_id",
    "CanonicalizationVerdict",
    "CanonicalizationResult",
    # Verify (HYBRID — InputDigestResult, VerifyReason, verify_input_digest)
    "verify_input_digest",
    "InputDigestResult",
    "VerifyReason",
    # Disclosure envelope
    "build_disclosure_envelope",
    "DisclosureError",
    # Signing — seal()'s producer Signer seam (self-attested signature)
    "Signer",
    "LocalKeypairSigner",
    "RotationRecord",
    "verify_capsule_signature",
    "verify_store_signed",
]
