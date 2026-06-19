# SPDX-License-Identifier: BSD-3-Clause
"""capsule-emit — one-call emit() for Agent Action Capsules.

The adoption surface for the Agent Action Capsule standard:

    from capsule_emit import emit

    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
        agent_output=result,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_po", "status": "dispatched"},
    )
    print(cap.capsule_id, cap.anchored)

Anchor is on by default (async, digest-only). Ledger is written to
``ledger.jsonl`` by default. Both are configurable.
"""
from .core import EmitResult, emit
from .ledger import append_to_ledger, read_ledger, view as ledger_view
from .manifest import ManifestDeclaration, find_manifest, load_manifest

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Core
    "emit",
    "EmitResult",
    # Ledger
    "append_to_ledger",
    "read_ledger",
    "ledger_view",
    # Manifest
    "load_manifest",
    "find_manifest",
    "ManifestDeclaration",
]
