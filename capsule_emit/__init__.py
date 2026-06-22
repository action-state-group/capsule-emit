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
from .core import EmitResult, emit
from .ledger import append_to_ledger, read_ledger
from .ledger import show as ledger_show
from .ledger import view as ledger_view
from .ledger import view_chains as ledger_view_chains
from .manifest import ManifestDeclaration, find_manifest, load_manifest
from .verify import verify_input_digest

__version__ = "0.1.1"

__all__ = [
    "__version__",
    # Core
    "emit",
    "EmitResult",
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
    # Verify
    "verify_input_digest",
]
