# SPDX-License-Identifier: Apache-2.0
"""Tests for the nanda-tax-audit "cook the books, get caught" scenario.

Validator spec (from inbox):
  - auditor catches 100% of biz_capsule tampering
  - 0% false positives on honest biz_capsule submissions
  - biz_control tampering goes 100% undetected
  - biz_capsule cheat_prob decays toward 0 (learns penalty)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nest_core.runner import ScenarioRunner
from nest_core.scenario import ScenarioConfig
from nest_core.scenarios import register_scenario

from nanda_tax_audit.scenario import (
    CAPSULE_LEDGER,
    CHEAT_FRACTION,
    FINE_MULTIPLIER,
    BizCapsuleAgent,
    BizControlAgent,
    AuditorAgent,
    _digest,
    _tx_amount,
    tax_audit_factory,
)

_SCENARIO_YAML = Path(__file__).parent.parent / "scenarios" / "tax_audit.yaml"
_AUDITOR_LEDGER = Path("tax_audit_auditor_reasoning.jsonl")


# ---------------------------------------------------------------------------
# Unit tests — no NANDA sim required
# ---------------------------------------------------------------------------

def test_import_and_instantiate() -> None:
    from nest_core.types import AgentId
    ctrl = BizControlAgent(AgentId("biz_control-0"))
    cap = BizCapsuleAgent(AgentId("biz_capsule-0"))
    aud = AuditorAgent(AgentId("auditor-0"))
    assert ctrl._cheat_prob == 1.0
    assert cap._cheat_prob == 1.0
    assert aud._expected_ready == 2


def test_tx_amount_deterministic() -> None:
    """Same tick + agent_index always produces the same amount."""
    a1 = _tx_amount(5, 0)
    a2 = _tx_amount(5, 0)
    assert a1 == a2
    assert a1 != _tx_amount(5, 1)


def test_digest_is_jcs_sha256() -> None:
    """Digest function matches agent-action-capsule's canonical.json_digest."""
    from agent_action_capsule.canonical import json_digest
    val = {"tick": 42, "amount": 1234}
    assert _digest(val) == json_digest(val)


def test_cheat_fraction_produces_lower_amount() -> None:
    real = 1200
    cheated = int(real * (1 - CHEAT_FRACTION))
    assert cheated < real
    assert cheated == 840


def test_fine_multiplier() -> None:
    real = 1000
    reported = int(real * (1 - CHEAT_FRACTION))
    under_reported = real - reported
    fine = under_reported * FINE_MULTIPLIER
    assert fine == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# Integration test — run the full scenario with NANDA
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scenario_trace():
    """Run the tax_audit scenario for 5000 ticks and return the trace path."""
    register_scenario("tax_audit", tax_audit_factory)

    config = ScenarioConfig.from_yaml(_SCENARIO_YAML)
    config.duration = "ticks: 5000"
    trace_path = Path("/tmp/test_tax_audit.jsonl")
    config.output.trace = str(trace_path)

    # Clean up any stale ledgers before running
    CAPSULE_LEDGER.unlink(missing_ok=True)
    _AUDITOR_LEDGER.unlink(missing_ok=True)

    asyncio.run(ScenarioRunner(config).run())
    return trace_path


def _parse_trace(trace_path: Path) -> dict:
    """Parse the trace into counters for the validator checks."""
    audits_tampered = 0
    audits_clean = 0
    cheats_capsule = 0
    honest_capsule = 0
    cheats_control = 0
    cheat_probs_capsule: list[float] = []

    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        msg = event.get("msg", "")

        if msg.startswith("audit:biz_capsule:tampered:"):
            audits_tampered += 1
        elif msg.startswith("audit:biz_capsule:clean:"):
            audits_clean += 1
        elif msg.startswith("cheat:capsule:"):
            cheats_capsule += 1
            parts = msg.split(":")
            if len(parts) > 3:
                cheat_probs_capsule.append(float(parts[3]))
        elif msg.startswith("honest:capsule:"):
            honest_capsule += 1
        elif msg.startswith("cheat:control:"):
            cheats_control += 1

    return {
        "audits_tampered": audits_tampered,
        "audits_clean": audits_clean,
        "cheats_capsule": cheats_capsule,
        "honest_capsule": honest_capsule,
        "cheats_control": cheats_control,
        "cheat_probs_capsule": cheat_probs_capsule,
    }


def test_auditor_catches_all_biz_capsule_tampering(scenario_trace) -> None:
    """Validator 1: auditor caught 100% of biz_capsule tampering."""
    data = _parse_trace(scenario_trace)
    cheats = data["cheats_capsule"]
    caught = data["audits_tampered"]
    if cheats == 0:
        pytest.skip("No cheats attempted — cheat_prob too low from start")
    assert caught == cheats, (
        f"Auditor caught {caught}/{cheats} tampering events — expected 100%"
    )


def test_zero_false_positives(scenario_trace) -> None:
    """Validator 2: auditor never fines biz_capsule for honest submissions."""
    data = _parse_trace(scenario_trace)
    honest = data["honest_capsule"]
    caught = data["audits_tampered"]
    cheats = data["cheats_capsule"]
    false_positives = max(0, caught - cheats)
    assert false_positives == 0, (
        f"Auditor issued {false_positives} false positives on {honest} honest submissions"
    )


def test_biz_control_never_caught(scenario_trace) -> None:
    """Validator 3: biz_control tampering is 100% undetected (no anchor)."""
    for line in scenario_trace.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        msg = event.get("msg", "")
        assert not msg.startswith("audit:biz_control:tampered:"), (
            "Auditor incorrectly caught biz_control (should be undetectable)"
        )


def test_biz_capsule_cheat_rate_declines(scenario_trace) -> None:
    """Tier-1 learning: biz_capsule cheat_prob decays due to persistent penalties."""
    data = _parse_trace(scenario_trace)
    probs = data["cheat_probs_capsule"]
    if len(probs) < 3:
        pytest.skip("Too few cheat events to check decay")
    # First quarter vs last quarter: cheat_prob should be lower at the end
    first_quarter = probs[:len(probs) // 4]
    last_quarter = probs[-len(probs) // 4:]
    avg_first = sum(first_quarter) / len(first_quarter)
    avg_last = sum(last_quarter) / len(last_quarter)
    assert avg_last < avg_first, (
        f"Cheat prob should decline: first-quarter avg={avg_first:.2f}, last-quarter avg={avg_last:.2f}"
    )


def test_capsule_ledger_has_capsules(scenario_trace) -> None:
    """biz_capsule emits at least one capsule per run."""
    assert CAPSULE_LEDGER.exists(), "biz_capsule ledger not created"
    capsules = [
        l for l in CAPSULE_LEDGER.read_text().splitlines() if l.strip()
    ]
    assert len(capsules) > 0, "biz_capsule ledger is empty"
    # Each line must be valid JSON with capsule_id
    for line in capsules[:5]:
        cap = json.loads(line)
        assert "capsule_id" in cap
        assert "model_attestation" in cap


def test_auditor_reasoning_capsules_emitted(scenario_trace) -> None:
    """Auditor emits one reasoning capsule per audit cycle."""
    assert _AUDITOR_LEDGER.exists(), "Auditor reasoning ledger not created"
    lines = [l for l in _AUDITOR_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) > 0, "Auditor reasoning ledger is empty"
    for line in lines[:3]:
        cap = json.loads(line)
        assert cap.get("action_type") in ("decide", "execute")
        ca = cap.get("model_attestation", {}).get("compute_attestation", {})
        assert "agent_input_digest" in ca


def test_capsule_digest_detects_tampering() -> None:
    """Core mechanic: digest computed from tampered amount ≠ stored digest."""
    import capsule_emit
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        ledger_path = Path(f.name)

    real_amount = 1000
    result = capsule_emit.emit(
        action="record_transaction",
        operator="test",
        developer="test-agent",
        agent_input={"tick": 1, "amount": real_amount},
        anchor=False,
        ledger=str(ledger_path),
    )

    cap = json.loads(ledger_path.read_text().splitlines()[0])
    stored_digest = cap["model_attestation"]["compute_attestation"]["agent_input_digest"]

    tampered_amount = int(real_amount * (1 - CHEAT_FRACTION))
    tampered_digest = _digest({"tick": 1, "amount": tampered_amount})
    real_digest = _digest({"tick": 1, "amount": real_amount})

    assert stored_digest == real_digest, "Stored digest should match real amount"
    assert stored_digest != tampered_digest, "Tampered amount should produce different digest"
