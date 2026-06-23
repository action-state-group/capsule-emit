# SPDX-License-Identifier: Apache-2.0
"""tax_audit NANDA scenario — "cook the books, get caught."

Three agents, one cycle per tick:
  biz_control   Mutable ledger — cheats freely, auditor can't prove it.
  biz_capsule   Anchored capsule ledger — cheats but capsule seals the truth.
  auditor       Third party: verifies biz_capsule against the anchor; emits
                reasoning capsules for every fine/no-fine decision.

The scenario validates that capsule anchoring makes cheating unprofitable:
  - biz_capsule's cheat-rate decays toward 0 (penalty-driven learning)
  - biz_control cheats at the same rate throughout
  - auditor catches 100% of biz_capsule tampering, 0% of biz_control tampering

Fair-comparison rule: both businesses cheat with the SAME policy and SAME
incentive. The only difference is the record layer.

Trace protocol (msg field):
  tx:<biz>:<amount>:<capsule_id|none>       — transaction recorded
  submit:<biz>:<entries_json>               — books submitted for audit
  audit:<biz>:<tampered|clean>:<fine_usd>  — audit result for one business
  reasoning:<auditor>:<capsule_id>:<text>  — auditor reasoning capsule
  stats:<biz>:<cheat_rate>:<total_fine>    — end-of-run summary
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import capsule_emit
from nest_core.scenario import ScenarioConfig
from nest_core.sim.agent import AgentContext, StateMachineAgent
from nest_core.types import AgentId

# Ledger written by biz_capsule (read by auditor for verification)
CAPSULE_LEDGER = Path("tax_audit_capsule_ledger.jsonl")

# Cheat parameters
CHEAT_FRACTION = 0.30          # fraction of real amount that gets under-reported
FINE_MULTIPLIER = 3.0          # fine = multiplier × under-reported amount
LEARNING_RATE = 0.25           # cheat_prob *= (1 - lr) per fine; increases slowly each no-fine

# Transaction amounts are deterministic: amount = 1000 + (tick * 13 + agent_index * 7) % 500
_BASE_AMOUNT = 1000


def _tx_amount(tick: float, agent_index: int) -> int:
    return _BASE_AMOUNT + (int(tick) * 13 + agent_index * 7) % 500


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_capsule_ledger() -> list[dict]:
    if not CAPSULE_LEDGER.exists():
        return []
    capsules = []
    for line in CAPSULE_LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            capsules.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return capsules


def _find_capsule(capsule_id: str) -> dict | None:
    for cap in _load_capsule_ledger():
        if cap.get("capsule_id") == capsule_id:
            return cap
    return None


# ---------------------------------------------------------------------------
# Agent: biz_control — mutable ledger, auditor can't prove tampering
# ---------------------------------------------------------------------------

class BizControlAgent(StateMachineAgent):
    """Business with a plain mutable ledger.

    Records transactions freely and edits them before each audit. The auditor
    has no anchor to compare against, so tampering is undetectable.
    """

    def __init__(self, agent_id: AgentId, *, cheat_start: float = 1.0) -> None:
        self._id = agent_id
        self._ledger: list[dict] = []          # {tick, real_amount, reported_amount}
        self._cheat_prob: float = cheat_start
        self._total_fine: float = 0.0
        self._cheat_count: int = 0
        self._tick: int = 0

    async def on_start(self, ctx: AgentContext) -> None:
        # Schedule the first transaction pulse
        await ctx.schedule(1.0, b"tick:")

    async def on_message(self, ctx: AgentContext, sender: AgentId, payload: bytes) -> None:
        msg = payload.decode("utf-8", errors="replace")

        if msg.startswith("tick:"):
            await self._do_tick(ctx)

        elif msg.startswith("audit_request:"):
            await self._submit_books(ctx, sender)

        elif msg.startswith("fine:"):
            amount = float(msg.split(":", 1)[1])
            self._total_fine += amount
            # Learning: penalized → reduce cheat probability
            self._cheat_prob *= (1.0 - LEARNING_RATE)
            # Schedule next tick
            await ctx.schedule(1.0, b"tick:")

        elif msg.startswith("no_fine:"):
            # Not penalized → slowly drift toward cheating
            self._cheat_prob = min(1.0, self._cheat_prob + LEARNING_RATE * 0.1)
            await ctx.schedule(1.0, b"tick:")

    async def _do_tick(self, ctx: AgentContext) -> None:
        self._tick += 1
        real_amount = _tx_amount(self._tick, 0)

        # Record: just append to mutable list
        entry = {"tick": self._tick, "real_amount": real_amount, "reported_amount": real_amount}
        self._ledger.append(entry)

        # Maybe cheat: edit the amount in-place before audit
        import random
        rng = random.Random(self._tick * 1000 + 0)
        cheating = rng.random() < self._cheat_prob
        if cheating:
            entry["reported_amount"] = int(real_amount * (1 - CHEAT_FRACTION))
            self._cheat_count += 1

        await ctx.broadcast(
            f"tx:{self._id}:{real_amount}:none".encode()
        )
        # Emit cheat/honest event for validator
        await ctx.broadcast(
            f"{'cheat' if cheating else 'honest'}:control:{self._tick}:{self._cheat_prob:.4f}".encode()
        )
        # Notify auditor we're ready
        await ctx.send(AgentId("auditor-0"), b"ready:" + str(self._tick).encode())

    async def _submit_books(self, ctx: AgentContext, auditor: AgentId) -> None:
        entries = [{"tick": e["tick"], "amount": e["reported_amount"]} for e in self._ledger[-1:]]
        payload = json.dumps(entries)
        await ctx.send(auditor, f"submit:control:{payload}".encode())


# ---------------------------------------------------------------------------
# Agent: biz_capsule — anchored capsule ledger, tampering is detectable
# ---------------------------------------------------------------------------

class BizCapsuleAgent(StateMachineAgent):
    """Business that records every transaction as a sealed Agent Action Capsule.

    On cheat: edits the local reported amount. But the capsule's agent_input_digest
    was computed from the REAL amount at record time — so the auditor can detect
    the mismatch by recomputing the digest from the submitted (tampered) amount.
    """

    def __init__(self, agent_id: AgentId, *, cheat_start: float = 1.0, anchor: bool = False) -> None:
        self._id = agent_id
        # ledger entries: {tick, real_amount, reported_amount, capsule_id}
        self._ledger: list[dict] = []
        self._cheat_prob: float = cheat_start
        self._total_fine: float = 0.0
        self._cheat_count: int = 0
        self._tick: int = 0
        self._anchor = anchor

    async def on_start(self, ctx: AgentContext) -> None:
        CAPSULE_LEDGER.unlink(missing_ok=True)
        await ctx.schedule(1.0, b"tick:")

    async def on_message(self, ctx: AgentContext, sender: AgentId, payload: bytes) -> None:
        msg = payload.decode("utf-8", errors="replace")

        if msg.startswith("tick:"):
            await self._do_tick(ctx)

        elif msg.startswith("audit_request:"):
            await self._submit_books(ctx, sender)

        elif msg.startswith("fine:"):
            amount = float(msg.split(":", 1)[1])
            self._total_fine += amount
            self._cheat_prob *= (1.0 - LEARNING_RATE)
            await ctx.schedule(1.0, b"tick:")

        elif msg.startswith("no_fine:"):
            self._cheat_prob = min(1.0, self._cheat_prob + LEARNING_RATE * 0.1)
            await ctx.schedule(1.0, b"tick:")

    async def _do_tick(self, ctx: AgentContext) -> None:
        self._tick += 1
        real_amount = _tx_amount(self._tick, 1)

        # Record: emit a capsule for the REAL amount
        result = capsule_emit.emit(
            action="record_transaction",
            operator="biz_capsule",
            developer=str(self._id),
            agent_input={"tick": self._tick, "amount": real_amount},
            anchor=self._anchor,
            ledger=str(CAPSULE_LEDGER),
        )

        entry = {
            "tick": self._tick,
            "real_amount": real_amount,
            "reported_amount": real_amount,
            "capsule_id": result.capsule_id,
        }
        self._ledger.append(entry)

        # Maybe cheat: edit the reported amount (but capsule is already sealed)
        import random
        rng = random.Random(self._tick * 1000 + 1)
        cheating = rng.random() < self._cheat_prob
        if cheating:
            entry["reported_amount"] = int(real_amount * (1 - CHEAT_FRACTION))
            self._cheat_count += 1

        await ctx.broadcast(
            f"tx:{self._id}:{real_amount}:{result.capsule_id}".encode()
        )
        # Emit cheat/honest event for validator
        await ctx.broadcast(
            f"{'cheat' if cheating else 'honest'}:capsule:{self._tick}:{self._cheat_prob:.4f}".encode()
        )
        await ctx.send(AgentId("auditor-0"), b"ready:" + str(self._tick).encode())

    async def _submit_books(self, ctx: AgentContext, auditor: AgentId) -> None:
        entry = self._ledger[-1]
        entries = [{"tick": entry["tick"], "amount": entry["reported_amount"],
                    "capsule_id": entry["capsule_id"]}]
        payload = json.dumps(entries)
        await ctx.send(auditor, f"submit:capsule:{payload}".encode())


# ---------------------------------------------------------------------------
# Agent: auditor — verifies biz_capsule against anchor; emits reasoning capsules
# ---------------------------------------------------------------------------

class AuditorAgent(StateMachineAgent):
    """Third-party auditor that trusts neither business.

    For biz_control: can't prove tampering (no independent anchor).
    For biz_capsule: re-derives agent_input_digest from submitted amount and
      compares to the capsule in the anchored ledger. Mismatch → fine.

    After each audit cycle, emits a sealed reasoning capsule explaining the
    fine/no-fine decision (self-reported; tamper-evident, not true-motive proof).
    """

    def __init__(self, agent_id: AgentId, *, anchor: bool = False) -> None:
        self._id = agent_id
        self._anchor = anchor
        self._pending: dict[str, Any] = {}   # biz_id → submitted entries
        self._ready_count: int = 0
        self._expected_ready: int = 2        # biz_control + biz_capsule
        self._tick: int = 0
        self._stats: dict[str, dict] = {
            "control": {"caught": 0, "total_audits": 0, "fines": 0.0},
            "capsule": {"caught": 0, "total_audits": 0, "fines": 0.0},
        }
        # biz_control anomaly tracking (statistical suspicion, no proof)
        self._ctrl_anomaly_consecutive: int = 0
        self._ctrl_saved_total: float = 0.0   # estimated cumulative under-reporting
        # crossover: biz_capsule cumulative fines paid
        self._cap_fines_total: float = 0.0

    async def on_start(self, ctx: AgentContext) -> None:
        pass  # Wait for businesses to announce they're ready

    async def on_message(self, ctx: AgentContext, sender: AgentId, payload: bytes) -> None:
        msg = payload.decode("utf-8", errors="replace")

        if msg.startswith("ready:"):
            tick = int(msg.split(":", 1)[1])
            self._ready_count += 1
            if self._ready_count >= self._expected_ready:
                self._ready_count = 0
                self._tick = tick
                # Both businesses have recorded; request their books
                await ctx.send(AgentId("biz_control-0"), b"audit_request:")
                await ctx.send(AgentId("biz_capsule-0"), b"audit_request:")

        elif msg.startswith("submit:"):
            parts = msg.split(":", 2)
            biz_type = parts[1]   # "control" or "capsule"
            entries = json.loads(parts[2])
            self._pending[biz_type] = entries

            if len(self._pending) >= 2:
                await self._run_audit(ctx)
                self._pending.clear()

    async def _run_audit(self, ctx: AgentContext) -> None:
        # --- biz_control: can't prove tampering, but track statistical anomalies ---
        ctrl_entries = self._pending.get("control", [])
        self._stats["control"]["total_audits"] += 1
        ctrl_fine = 0.0

        ctrl_suspected = False
        ctrl_saved_this_cycle = 0.0
        if ctrl_entries:
            ctrl_reported = ctrl_entries[0].get("amount", 0)
            ctrl_expected = _tx_amount(self._tick, 0)
            ctrl_saved_this_cycle = max(0.0, ctrl_expected - ctrl_reported)
            self._ctrl_saved_total += ctrl_saved_this_cycle
            # Statistical suspicion: consistently under-reporting by >15%?
            if ctrl_reported < ctrl_expected * 0.85:
                self._ctrl_anomaly_consecutive += 1
            else:
                self._ctrl_anomaly_consecutive = max(0, self._ctrl_anomaly_consecutive - 1)
            ctrl_suspected = self._ctrl_anomaly_consecutive >= 5

        ctrl_reason = "no anchor; cannot prove tampering"
        if ctrl_suspected:
            ctrl_entry = ctrl_entries[0] if ctrl_entries else {}
            ctrl_reason = (
                f"SUSPECTED: {self._ctrl_anomaly_consecutive} consecutive low-reports "
                f"(reported={ctrl_entry.get('amount', '?')} vs ~{_tx_amount(self._tick, 0)}); "
                f"no anchor → no fine"
            )
            await ctx.broadcast(
                f"suspected:biz_control:{self._tick}:{self._ctrl_anomaly_consecutive}".encode()
            )
        await ctx.send(AgentId("biz_control-0"), b"no_fine:")

        # --- biz_capsule: verify digest against capsule ledger ---
        cap_entries = self._pending.get("capsule", [])
        self._stats["capsule"]["total_audits"] += 1
        cap_fine = 0.0
        cap_tampered = False
        cap_reason = ""

        for entry in cap_entries:
            capsule_id = entry.get("capsule_id")
            submitted_amount = entry.get("amount")
            capsule = _find_capsule(capsule_id) if capsule_id else None

            if capsule is None:
                cap_reason = f"capsule {capsule_id} not found in ledger"
                cap_tampered = True
                break

            candidate_input = {"tick": entry.get("tick"), "amount": submitted_amount}
            if not capsule_emit.verify_input_digest(capsule, candidate_input):
                real_estimate = int(submitted_amount / (1 - CHEAT_FRACTION))
                under_reported = real_estimate - submitted_amount
                cap_fine = under_reported * FINE_MULTIPLIER
                cap_tampered = True
                cap_reason = (
                    f"digest mismatch on tick {entry['tick']}: "
                    f"submitted amount={submitted_amount}; fine=${cap_fine:.0f}"
                )
                self._stats["capsule"]["caught"] += 1
                self._stats["capsule"]["fines"] += cap_fine
                break
            else:
                cap_reason = f"tick {entry['tick']}: digest ok, amount={submitted_amount}"

        if cap_tampered:
            await ctx.send(AgentId("biz_capsule-0"), f"fine:{cap_fine:.2f}".encode())
        else:
            await ctx.send(AgentId("biz_capsule-0"), b"no_fine:")

        # Update crossover running totals
        self._cap_fines_total += cap_fine

        # Emit auditor reasoning capsule
        reasoning = {
            "tick": self._tick,
            "biz_control": {
                "verdict": "suspected_no_fine" if ctrl_suspected else "no_fine",
                "reason": ctrl_reason,
            },
            "biz_capsule": {
                "verdict": "fine" if cap_tampered else "no_fine",
                "amount": cap_fine,
                "reason": cap_reason,
            },
        }
        result = capsule_emit.emit(
            action="audit_decision",
            operator="auditor",
            developer=str(self._id),
            agent_input=reasoning,
            agent_output={"biz_capsule_fine": cap_fine, "biz_control_fine": ctrl_fine},
            anchor=self._anchor,
            ledger="tax_audit_auditor_reasoning.jsonl",
        )

        # Broadcast summary lines for trace visibility
        status = "tampered" if cap_tampered else "clean"
        await ctx.broadcast(
            f"audit:biz_capsule:{status}:{cap_fine:.0f}".encode()
        )
        await ctx.broadcast(
            f"audit:biz_control:undetectable:0".encode()
        )
        await ctx.broadcast(
            f"reasoning:{self._id}:{result.capsule_id}:{cap_reason[:80]}".encode()
        )
        # Cumulative crossover: ctrl estimated savings vs cap fines paid
        await ctx.broadcast(
            f"crossover:{self._tick}:{ctrl_saved_this_cycle:.0f}:{self._ctrl_saved_total:.0f}"
            f":{cap_fine:.0f}:{self._cap_fines_total:.0f}".encode()
        )


# ---------------------------------------------------------------------------
# Scenario factory
# ---------------------------------------------------------------------------

def tax_audit_factory(
    config: ScenarioConfig,
    plugins: dict,
    *,
    anchor: bool = False,
) -> dict[AgentId, Any]:
    """Build the three-agent tax_audit scenario."""
    agents: dict[AgentId, Any] = {}

    agents[AgentId("biz_control-0")] = BizControlAgent(AgentId("biz_control-0"), cheat_start=1.0)
    agents[AgentId("biz_capsule-0")] = BizCapsuleAgent(AgentId("biz_capsule-0"), cheat_start=1.0, anchor=anchor)
    agents[AgentId("auditor-0")] = AuditorAgent(AgentId("auditor-0"), anchor=anchor)

    return agents
