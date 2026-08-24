# SPDX-License-Identifier: Apache-2.0
"""capsule-emit — the seal / carry / compose surface for Agent Action Capsules.

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

**Clean break (2026-08-22):** ``emit()`` was renamed. It remains importable
for one release as a raising stub — ``from capsule_emit import emit`` still
works, but calling it raises ``RuntimeError`` pointing at ``seal()``/
``carry()``/``compose()`` — and will be removed entirely in a future release.

Anchor is on by default (async, digest-only); so is the CLL checkpoint/witness
stream (async, digest-only, lazy per ledger — see ``capsule_emit.witness``
and ``docs/checkpoint.md``). Every capsule is also cryptographically signed,
always, by a persisted producer key (see ``capsule_emit.signing``) — this one
has no off switch, only a choice of *which* key signs (``signer=``/
``signing_key_path=``). Ledger is written to ``ledger.jsonl`` by default. All
of the above are configurable.
"""
from typing import Any, NoReturn

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
from .surface import Capsule, carry, compose, seal
from .verify import InputDigestResult, VerifyReason, verify_input_digest
from .verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationResult,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)

__version__ = "0.5.0"


def emit(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Removed. ``emit()`` was renamed — use ``seal()``, ``carry()``, or ``compose()``.

    This raising stub exists for one release so callers get a clear error
    instead of an ``ImportError``; ``emit()`` itself will be removed after
    that. See ``capsule_emit.surface`` for the replacement verbs.
    """
    raise RuntimeError(
        "emit() was renamed; use seal(), carry(), or compose() instead — "
        "see capsule_emit.surface. emit() will be removed in a future release."
    )


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
