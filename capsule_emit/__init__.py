# SPDX-License-Identifier: Apache-2.0
"""capsule-emit — one-call emit() for Agent Action Capsules.

The adoption surface for the Agent Action Capsule standard:

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

Anchor is on by default (async, digest-only). Ledger is written to
``ledger.jsonl`` by default. Both are configurable.
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
from .ledger import append_to_ledger, read_ledger
from .ledger import show as ledger_show
from .ledger import view as ledger_view
from .ledger import view_chains as ledger_view_chains
from .manifest import ManifestDeclaration, find_manifest, load_manifest
from .numbers import CANONICALIZATION_ID
from .verify import verify_input_digest
from .verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationResult,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)

__version__ = "0.3.1"

__all__ = [
    "__version__",
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
    # Canonicalization id constant + verifier
    "CANONICALIZATION_ID",
    "KNOWN_ALGORITHMS",
    "verify_canonicalization_id",
    "CanonicalizationVerdict",
    "CanonicalizationResult",
    # Verify
    "verify_input_digest",
    # Disclosure envelope
    "build_disclosure_envelope",
    "DisclosureError",
]
