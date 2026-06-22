# SPDX-License-Identifier: Apache-2.0
"""CapsuleEmitTrust — NANDA Town Trust layer plugin backed by capsule-emit.

Drop-in replacement for ``agent_receipts``: every interaction a NANDA agent
already reports via ``ctx.plugins.get("trust").report(...)`` is anchored to
an Agent Action Capsule ledger — zero agent-code changes required.

Three gates for a receipt to build reputation, same as ``agent_receipts``:

1. **Valid** — Ed25519 issuer signature verifies.
2. **Corroborated** — distinct counterparty co-signed the same interaction.
3. **Anchored** — a capsule was emitted for this receipt and is present in the
   capsule ledger (the public time-anchor gate ``agent_receipts`` can't enforce).

Gate 3 is the additive capsule contribution: an agent whose interactions are
never anchored (because e.g. the emitter was offline) gets no reputation score
even if their receipts are individually valid and corroborated. The capsule
ledger is the authoritative record, independently verifiable by any party who
ran none of the agents.

Plain-string ``evidence.detail`` (stock NANDA scenarios with no receipt) falls
back to the ``score_average`` heuristic so this plugin stays a drop-in in any
scenario.

Registered under ``("trust", "capsule_emit")`` in ``nest.plugins.trust``.

Example::

    trust = CapsuleEmitTrust()
    await trust.report(AgentId("a1"), evidence)
    rep = await trust.score(AgentId("a1"))
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import capsule_emit
from nest_core.types import AgentId, Attestation, Claim, Evidence, ReputationScore
# Private helpers from nest-plugins-reference — may break if NANDA refactors
# agent_receipts.  Sentinel test: tests/test_capsule_emit_trust.py::test_private_import_still_works
try:
    from nest_plugins_reference.trust.agent_receipts import (
        NORMALIZATION_K,
        DEFAULT_CATEGORY_WEIGHTS,
        _action_field,
        _counterparty,
        _effective_receipts,
        _normalize,
        _raw_reputation,
        _verify_receipt,
        did_for_pubkey,
        is_corroborated,
    )
except ImportError as _exc:
    raise ImportError(
        "nanda-trust-capsule requires nest-plugins-reference; "
        "run: pip install nest-plugins-reference"
    ) from _exc

logger = logging.getLogger(__name__)

ALGORITHM = "ed25519"

# Capsule action name for interactions where the receipt category is absent or
# unrecognised. Using "message_sent" keeps the capsule_id stable across runs
# while mapping to the lowest weight in the category table (1.0).
_DEFAULT_CAPSULE_ACTION = "message_sent"


class CapsuleEmitTrust:
    """Anchored, collusion-resistant reputation implementing the ``Trust`` Protocol.

    Mirrors ``AgentReceiptsTrust`` with one addition: every corroborated receipt
    triggers a ``capsule_emit.emit()`` call, so the reputation history is sealed
    to an Agent Action Capsule ledger that any third party can verify independently
    with ``agent-action-capsule verify --store <ledger_path>``.

    Args:
        anchor: Whether to anchor capsules to the public log (default False —
            deterministic replay; set True for the live-anchor money-shot pass).
        ledger: Path for the capsule ledger JSONL file.

    Example::

        trust = CapsuleEmitTrust(anchor=False)
        rep = await trust.score(AgentId("a1"))
    """

    _SYSTEM_AGENT = AgentId("trust:capsule_emit")

    def __init__(
        self,
        identity: Any = None,
        *,
        anchor: bool = False,
        ledger: str | Path = "capsule_ledger.jsonl",
    ) -> None:
        self._identity = identity
        self._anchor = anchor
        self._ledger_path = Path(ledger)
        self._system_seed = hashlib.sha256(b"trust:capsule_emit").digest()[:32]
        # In-memory receipt ledger: same structure as agent_receipts, used for
        # corroboration and severance. The capsule ledger is the public record.
        self._receipts: list[dict[str, Any]] = []
        # receipt_key (issuer_did, counterparty_did, action_id) → capsule_id.
        # Tracks which receipts have been anchored so score() applies gate 3.
        self._anchored: dict[tuple[str, str, str], str] = {}
        # Plain-string fallback scores (stock-scenario compatibility).
        self._fallback_scores: dict[AgentId, list[float]] = {}
        self._stakes: dict[AgentId, int] = {}

    def _did_of(self, agent: AgentId) -> str:
        """Map a NEST AgentId to its Ed25519 hex DID (deterministic).

        Example::

            did = trust._did_of(AgentId("honest-0"))
        """
        seed = hashlib.sha256(str(agent).encode()).digest()[:32]
        pub = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        return did_for_pubkey(pub)

    def _receipt_key(self, receipt: dict[str, Any]) -> tuple[str, str, str]:
        issuer = str(receipt.get("issuer_did", ""))
        cp = _counterparty(receipt) or ""
        action_id = str(_action_field(receipt, "action_id") or _action_field(receipt, "category") or "")
        return (issuer, cp, action_id)

    async def report(self, agent: AgentId, evidence: Evidence) -> None:
        """Report evidence, anchor to capsule ledger if it's a valid receipt.

        If ``evidence.detail`` decodes to a cross-signed receipt dict, the
        receipt is added to the in-memory ledger AND a capsule is emitted (off
        the event loop — does not block the sim). Plain-string detail uses the
        stock ``score_average`` heuristic so any scenario still works.

        Example::

            await trust.report(AgentId("a1"), Evidence(reporter=r, subject=s,
                kind="positive", detail=json.dumps(receipt)))
        """
        try:
            parsed: object = json.loads(evidence.detail)
        except (json.JSONDecodeError, TypeError):
            self._record_fallback(agent, evidence)
            return

        if not isinstance(parsed, dict):
            self._record_fallback(agent, evidence)
            return

        receipt = cast("dict[str, Any]", parsed)
        if not _verify_receipt(receipt):
            logger.debug(
                "report: receipt for agent=%s failed issuer-signature verification; "
                "using heuristic fallback",
                agent,
            )
            self._record_fallback(agent, evidence)
            return

        # Valid receipt: add to in-memory ledger.
        self._receipts.append(receipt)

        # Emit a capsule (async-safe: runs sync emit in thread to avoid blocking).
        await asyncio.to_thread(self._emit_capsule, agent, receipt)

    def _emit_capsule(self, agent: AgentId, receipt: dict[str, Any]) -> None:
        """Emit one capsule for a receipt, keyed by issuer DID and action."""
        issuer_did = str(receipt.get("issuer_did", ""))
        category = str(_action_field(receipt, "category") or _DEFAULT_CAPSULE_ACTION)
        cp_did = _counterparty(receipt) or ""
        action_id = str(_action_field(receipt, "action_id") or "")
        corroborated = is_corroborated(receipt)

        try:
            result = capsule_emit.emit(
                action=category,
                operator=issuer_did,
                developer=str(agent),
                agent_input=receipt,
                agent_output={"corroborated": corroborated, "counterparty_did": cp_did},
                anchor=self._anchor,
                ledger=str(self._ledger_path),
            )
            key = self._receipt_key(receipt)
            self._anchored[key] = result.capsule_id
        except Exception:
            logger.exception("capsule emit failed for agent=%s", agent)

    async def score(self, agent: AgentId) -> ReputationScore:
        """Reputation from corroborated, non-severed, anchored receipts.

        Same three-gate logic as ``agent_receipts``, plus gate 3: only receipts
        with an emitted capsule in ``_anchored`` count toward the score. Agents
        with no receipt history fall back to the plain-string heuristic, or the
        neutral prior (0.5).

        Example::

            rep = await trust.score(AgentId("a1"))
        """
        did = self._did_of(agent)
        # Gate 1+2: valid and corroborated, ring-severed.
        effective = _effective_receipts(self._receipts)
        mine_eff = [r for r in effective if str(r.get("issuer_did", "")) == did]

        # Gate 3: only count receipts that have a corresponding capsule.
        mine_anchored = [r for r in mine_eff if self._receipt_key(r) in self._anchored]
        mine_all = [r for r in self._receipts if str(r.get("issuer_did", "")) == did]

        if mine_all:
            raw = _raw_reputation(mine_anchored, DEFAULT_CATEGORY_WEIGHTS)
            confidence = len(mine_anchored) / len(mine_all) if mine_all else 0.0
            return ReputationScore(
                agent_id=agent,
                score=_normalize(raw),
                confidence=confidence,
                sample_count=len(mine_all),
            )

        fallback = self._fallback_scores.get(agent)
        if fallback:
            avg = sum(fallback) / len(fallback)
            return ReputationScore(
                agent_id=agent,
                score=avg,
                confidence=min(1.0, len(fallback) / 100.0),
                sample_count=len(fallback),
            )
        return ReputationScore(agent_id=agent, score=0.5, confidence=0.0, sample_count=0)

    async def attest(self, agent: AgentId, claim: Claim) -> Attestation:
        """Issue an Ed25519-signed attestation (same as agent_receipts).

        Example::

            att = await trust.attest(AgentId("a1"), claim)
        """
        from nest_core.types import Signature

        sk = Ed25519PrivateKey.from_private_bytes(self._system_seed)
        raw = sk.sign(claim.model_dump_json().encode())
        sig = Signature(signer=self._SYSTEM_AGENT, value=raw, algorithm=ALGORITHM)
        return Attestation(issuer=self._SYSTEM_AGENT, claim=claim, signature=sig)

    async def stake(self, agent: AgentId, amount: int) -> None:
        """Stake reputation on an agent (parity no-op).

        Example::

            await trust.stake(AgentId("a1"), 100)
        """
        self._stakes[agent] = self._stakes.get(agent, 0) + amount

    def _record_fallback(self, agent: AgentId, evidence: Evidence) -> None:
        score_val = 0.5
        if evidence.kind == "positive":
            score_val = 1.0
        elif evidence.kind in ("negative", "byzantine"):
            score_val = 0.0
        self._fallback_scores.setdefault(agent, []).append(score_val)
