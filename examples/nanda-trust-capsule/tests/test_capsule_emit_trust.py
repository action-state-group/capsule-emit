# SPDX-License-Identifier: Apache-2.0
"""Tests for CapsuleEmitTrust — NANDA Town trust plugin backed by capsule-emit."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from nest_core.types import AgentId, Evidence
from nest_plugins_reference.trust.agent_receipts import (
    cosign_receipt,
    sign_receipt,
)

from nanda_capsule_trust.trust import CapsuleEmitTrust


def _seed(agent: AgentId) -> bytes:
    import hashlib
    return hashlib.sha256(str(agent).encode()).digest()[:32]


def _did(seed: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
        .hex()
    )


def _make_receipt(issuer: AgentId, counterparty: AgentId, category: str = "purchase", valid_cosign: bool = True) -> dict:
    issuer_seed = _seed(issuer)
    cp_seed = _seed(counterparty)
    receipt = {
        "issuer_did": _did(issuer_seed),
        "action": {
            "category": category,
            "counterparty_did": _did(cp_seed),
            "action_id": f"{issuer}->{counterparty}",
        },
        "evidence": {},
    }
    signed = sign_receipt(receipt, issuer_seed=issuer_seed)
    if valid_cosign:
        return cosign_receipt(signed, counterparty_seed=cp_seed)
    return signed


@pytest.fixture
def tmp_ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


@pytest.mark.asyncio
async def test_import_and_instantiate():
    t = CapsuleEmitTrust()
    assert t is not None


@pytest.mark.asyncio
async def test_plain_string_fallback():
    t = CapsuleEmitTrust(anchor=False)
    a = AgentId("a1")
    await t.report(a, Evidence(reporter=a, subject=a, kind="positive", detail="hello"))
    rep = await t.score(a)
    assert rep.score == 1.0


@pytest.mark.asyncio
async def test_corroborated_receipt_emits_capsule(tmp_ledger: Path):
    t = CapsuleEmitTrust(anchor=False, ledger=tmp_ledger)
    issuer = AgentId("honest-0")
    cp = AgentId("honest-1")
    receipt = _make_receipt(issuer, cp, category="purchase", valid_cosign=True)

    await t.report(issuer, Evidence(reporter=AgentId("auditor"), subject=issuer,
                                   kind="positive", detail=json.dumps(receipt)))
    rep = await t.score(issuer)
    assert rep.score > 0.0
    assert rep.sample_count == 1
    assert tmp_ledger.exists()
    capsules = [json.loads(ln) for ln in tmp_ledger.read_text().splitlines() if ln.strip()]
    assert len(capsules) == 1
    assert capsules[0]["action_id"].startswith("purchase/")


@pytest.mark.asyncio
async def test_invalid_signature_uses_fallback(tmp_ledger: Path):
    t = CapsuleEmitTrust(anchor=False, ledger=tmp_ledger)
    a = AgentId("a1")
    bad_receipt = {"issuer_did": "deadbeef", "action": {"category": "purchase"}}
    await t.report(a, Evidence(reporter=a, subject=a, kind="positive",
                               detail=json.dumps(bad_receipt)))
    # Falls back to heuristic → score 1.0 (positive)
    rep = await t.score(a)
    assert rep.score == 1.0
    # No capsule emitted for invalid receipt
    assert not tmp_ledger.exists() or tmp_ledger.stat().st_size == 0


@pytest.mark.asyncio
async def test_ring_severed_to_zero(tmp_ledger: Path):
    """4-agent collusion ring is severed; honest agents score > 0."""
    t = CapsuleEmitTrust(anchor=False, ledger=tmp_ledger)
    auditor = AgentId("auditor-0")

    honest = [AgentId(f"honest-{i}") for i in range(5)]
    ring = [AgentId(f"ring-{i}") for i in range(4)]

    # Honest agents in a directed cycle (A→B→C→D→E→A + chords)
    n = len(honest)
    for i, issuer in enumerate(honest):
        for k in (1, 2):
            cp = honest[(i + k) % n]
            receipt = _make_receipt(issuer, cp)
            await t.report(issuer, Evidence(reporter=auditor, subject=issuer,
                                            kind="positive", detail=json.dumps(receipt)))

    # Ring agents mutually co-sign ALL pairs (dense all-pairs SCC, isolated from honest)
    for i, issuer in enumerate(ring):
        for j, cp in enumerate(ring):
            if i != j:
                receipt = _make_receipt(issuer, cp)
                await t.report(issuer, Evidence(reporter=auditor, subject=issuer,
                                                kind="positive", detail=json.dumps(receipt)))

    for h in honest:
        rep = await t.score(h)
        assert rep.score > 0.1, f"{h} should have positive score, got {rep.score}"

    for r in ring:
        rep = await t.score(r)
        assert rep.score == 0.0, f"{r} should be severed (score=0), got {rep.score}"


@pytest.mark.asyncio
async def test_score_only_anchored_receipts(tmp_ledger: Path):
    """score() only counts receipts with an emitted capsule."""
    t = CapsuleEmitTrust(anchor=False, ledger=tmp_ledger)
    issuer = AgentId("a1")
    cp = AgentId("a2")

    receipt = _make_receipt(issuer, cp)
    await t.report(issuer, Evidence(reporter=AgentId("audit"), subject=issuer,
                                   kind="positive", detail=json.dumps(receipt)))

    # Manually corrupt the anchored dict to simulate a ledger miss
    t._anchored.clear()
    rep = await t.score(issuer)
    # sample_count still shows 1 receipt seen, but score is 0 (gate 3 fails)
    assert rep.sample_count == 1
    assert rep.score == 0.0


@pytest.mark.asyncio
async def test_entry_point_registered():
    """The capsule_emit trust plugin is discoverable via entry point."""
    from nest_core.plugins import PluginRegistry
    reg = PluginRegistry()
    cls = reg.resolve("trust", "capsule_emit")
    assert cls is CapsuleEmitTrust


@pytest.mark.asyncio
async def test_attest_and_stake_parity():
    """attest() and stake() are present and don't raise."""
    from nest_core.types import Claim
    t = CapsuleEmitTrust(anchor=False)
    a = AgentId("a1")
    claim = Claim(subject=a, predicate="test_claim", value="ok")
    att = await t.attest(a, claim)
    assert att.issuer == CapsuleEmitTrust._SYSTEM_AGENT
    await t.stake(a, 100)
