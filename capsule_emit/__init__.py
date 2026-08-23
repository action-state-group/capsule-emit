# SPDX-License-Identifier: Apache-2.0
"""capsule-emit — one-call emit() for Agent Action Capsules.

The developer surface, on one authorship axis — never
``import capsule_emit as capsule`` (it would shadow the noun the canonical
line below assigns to):

    from capsule_emit import seal, carry, compose

    capsule = seal(payload)                        # MINT — mine
    effect  = carry(receipt_bytes)                 # CARRY — theirs, already signed
    action  = compose([auth, guard, act, effect])  # BIND — references members

All three return a :class:`Capsule` (never call it a ``receipt`` — that word
is reserved for what a witness/transparency-service returns) and all three
append to the log. See ``capsule_emit.surface`` for the full contract.

``emit()`` is the lower-level, fully-parameterized primitive ``seal()``
wraps (action/operator/developer/model/effect all in one call) and remains
available unchanged:

    from capsule_emit import emit

    cap = emit(
        action="write_order",
        operator="acme-co",
        developer="po-agent@v1",
        agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
        agent_output=result,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_order", "status": "dispatched"},
    )
    print(cap.capsule_id, cap.anchored)

Anchor is on by default (async, digest-only); so is the CLL checkpoint/witness
stream (async, digest-only, lazy per ledger — see ``capsule_emit.witness``
and ``docs/checkpoint.md``). Ledger is written to ``ledger.jsonl`` by
default. All of the above are configurable.
"""
from .approval import list_pending, seal_approval
from .core import EmitResult, emit
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
from .ledger import append_to_ledger, read_ledger
from .ledger import show as ledger_show
from .ledger import view as ledger_view
from .ledger import view_chains as ledger_view_chains
from .manifest import ManifestDeclaration, find_manifest, load_manifest
from .numbers import CANONICALIZATION_ID, float_to_str
from .surface import Capsule, carry, compose, seal
from .verify import InputDigestResult, VerifyReason, verify_input_digest
from .verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationResult,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)

__version__ = "0.5.0"

__all__ = [
    "__version__",
    # seal / carry / compose — the developer surface (Layer 0)
    "Capsule",
    "seal",
    "carry",
    "compose",
    # Core
    "emit",
    "EmitResult",
    # Approval record + pending
    "seal_approval",
    "list_pending",
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
]
