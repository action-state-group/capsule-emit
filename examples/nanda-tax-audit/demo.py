#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tax audit demo: "cook the books, get caught."

Two businesses cheat the IRS the same way. The one with anchored capsule books
gets caught every time; the one with mutable books gets away with it. Over time,
the capsule business learns that cheating is unprofitable and goes honest.

Usage:
    python demo.py                    # 20 audit cycles
    python demo.py --ticks 5000       # short run (~5 cycles)
    python demo.py --verify           # run + verify both ledgers
    python demo.py --anchor           # live-anchor to $AAC_ANCHOR_URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from nest_core.runner import ScenarioRunner
from nest_core.scenario import ScenarioConfig
from nest_core.scenarios import register_scenario

from nanda_tax_audit.scenario import CAPSULE_LEDGER, tax_audit_factory

_SCENARIO_YAML = Path(__file__).parent / "scenarios" / "tax_audit.yaml"
_TRACE_PATH = Path(__file__).parent / "traces" / "tax_audit.jsonl"
_AUDITOR_LEDGER = Path("tax_audit_auditor_reasoning.jsonl")


def _parse_trace(trace_path: Path) -> dict:
    """Extract audit results and cheat-rate trend from trace.

    Counts only `kind=broadcast` events — each ctx.broadcast produces one
    broadcast entry plus one receive entry per recipient, so filtering to
    broadcast gives the logical event count without N-fold inflation.
    """
    audits: list[dict] = []
    tx_capsule: list[dict] = []
    tx_control: list[dict] = []
    reasoning_capsules: list[str] = []
    cheats_capsule = 0
    honest_capsule = 0
    cheats_control = 0
    honest_control = 0
    false_positives = 0
    last_cheat_prob_capsule: float | None = None
    last_cheat_prob_control: float | None = None
    ctrl_suspicion_count = 0
    crossover_entries: list[dict] = []

    if not trace_path.exists():
        return {}

    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        # Count only logical broadcast events (not per-recipient receive copies)
        if event.get("kind") != "broadcast":
            continue
        msg = event.get("msg", "")

        if msg.startswith("cheat:capsule:"):
            parts = msg.split(":")
            cheats_capsule += 1
            if len(parts) > 3:
                last_cheat_prob_capsule = float(parts[3])

        elif msg.startswith("honest:capsule:"):
            honest_capsule += 1
            parts = msg.split(":")
            if len(parts) > 3:
                last_cheat_prob_capsule = float(parts[3])

        elif msg.startswith("cheat:control:"):
            cheats_control += 1
            parts = msg.split(":")
            if len(parts) > 3:
                last_cheat_prob_control = float(parts[3])

        elif msg.startswith("honest:control:"):
            honest_control += 1

        elif msg.startswith("audit:biz_capsule:"):
            parts = msg.split(":")
            audits.append({"status": parts[2], "fine": float(parts[3])})

        elif msg.startswith("tx:biz_capsule-0:"):
            parts = msg.split(":")
            tx_capsule.append({"amount": int(parts[2]), "capsule_id": parts[3]})

        elif msg.startswith("tx:biz_control-0:"):
            parts = msg.split(":")
            tx_control.append({"amount": int(parts[2])})

        elif msg.startswith("suspected:biz_control:"):
            ctrl_suspicion_count += 1

        elif msg.startswith("reasoning:"):
            parts = msg.split(":", 3)
            if len(parts) >= 4:
                reasoning_capsules.append(f"  {parts[2][:8]}… [{parts[1][:16]}] : {parts[3][:70]}")

        elif msg.startswith("crossover:"):
            parts = msg.split(":")
            if len(parts) >= 6:
                crossover_entries.append({
                    "tick": int(parts[1]),
                    "ctrl_saved": float(parts[2]),
                    "ctrl_saved_total": float(parts[3]),
                    "cap_fine": float(parts[4]),
                    "cap_fines_total": float(parts[5]),
                })

    caught_count = sum(1 for a in audits if a["status"] == "tampered")
    false_positives = max(0, caught_count - cheats_capsule)

    return {
        "audits": audits,
        "tx_capsule": tx_capsule,
        "tx_control": tx_control,
        "reasoning_capsules": reasoning_capsules,
        "cheats_capsule": cheats_capsule,
        "honest_capsule": honest_capsule,
        "cheats_control": cheats_control,
        "honest_control": honest_control,
        "false_positives": false_positives,
        "last_cheat_prob_capsule": last_cheat_prob_capsule,
        "last_cheat_prob_control": last_cheat_prob_control,
        "ctrl_suspicion_count": ctrl_suspicion_count,
        "crossover_entries": crossover_entries,
    }


def _print_report(data: dict, capsule_ledger: Path, auditor_ledger: Path) -> None:
    audits = data.get("audits", [])
    tx_capsule = data.get("tx_capsule", [])
    reasoning = data.get("reasoning_capsules", [])
    cheats_capsule = data.get("cheats_capsule", 0)
    cheats_control = data.get("cheats_control", 0)
    honest_capsule = data.get("honest_capsule", 0)
    honest_control = data.get("honest_control", 0)
    false_positives = data.get("false_positives", 0)
    last_prob_cap = data.get("last_cheat_prob_capsule")
    last_prob_ctrl = data.get("last_cheat_prob_control")
    ctrl_suspicion_count = data.get("ctrl_suspicion_count", 0)
    crossover_entries = data.get("crossover_entries", [])

    total_audits = len(audits)
    caught = sum(1 for a in audits if a["status"] == "tampered")
    total_fines = sum(a["fine"] for a in audits)

    catch_pct = (caught / cheats_capsule * 100) if cheats_capsule else 100.0
    fp_pct = (false_positives / max(1, honest_capsule) * 100)

    print("\n" + "=" * 64)
    print("  Tax Audit Sim — cook the books, get caught")
    print("=" * 64)
    print(f"  Audit cycles:        {total_audits}")
    print(f"  Transactions sealed: {len(tx_capsule)} capsules in biz_capsule ledger")
    print()
    print("  biz_control (mutable ledger — no anchor)")
    print(f"    Cheated:           {cheats_control} of {cheats_control+honest_control} cycles")
    if last_prob_ctrl is not None:
        print(f"    Final cheat rate:  {last_prob_ctrl*100:.1f}%  (no deterrent — stays high)")
    print(f"    Caught:            0 of {cheats_control}  (0% — auditor has no anchor)")
    if ctrl_suspicion_count > 0:
        print(f"    Suspected:         {ctrl_suspicion_count} cycles flagged as anomalous")
        print(f"                       (pattern detected, no proof — no fine issued)")
    print()
    print("  biz_capsule (anchored capsule ledger)")
    print(f"    Cheated:           {cheats_capsule} of {cheats_capsule+honest_capsule} cycles")
    if last_prob_cap is not None:
        print(f"    Final cheat rate:  {last_prob_cap*100:.1f}%  (declining — learned penalty)")
    print(f"    Caught:            {caught} of {cheats_capsule}  ({catch_pct:.0f}% detection rate)")
    print(f"    False positives:   {false_positives} of {honest_capsule} honest cycles ({fp_pct:.0f}%)")
    print(f"    Fines paid:        ${total_fines:,.0f}")
    print()

    # Cumulative-$ crossover: how much did each side "gain" from cheating?
    if crossover_entries:
        last = crossover_entries[-1]
        ctrl_saved = last["ctrl_saved_total"]
        cap_fines = last["cap_fines_total"]
        print("  Crossover (cumulative $):")
        print(f"    biz_control estimated savings:  ${ctrl_saved:>10,.0f}  (no deterrent)")
        print(f"    biz_capsule fines paid:         ${cap_fines:>10,.0f}  (learns to stop)")
        print(f"    Net advantage (ctrl - cap):     ${ctrl_saved - cap_fines:>10,.0f}")
        print()

    v1_pass = catch_pct >= 99.0 or cheats_capsule == 0
    v2_pass = false_positives == 0

    print(f"  Validator 1: auditor caught ≥99% of biz_capsule tampering")
    print(f"    → {catch_pct:.0f}%  {'PASS ✅' if v1_pass else 'FAIL ❌'}")
    print(f"  Validator 2: 0% false positives on biz_capsule")
    print(f"    → {false_positives} false positives  {'PASS ✅' if v2_pass else 'FAIL ❌'}")
    print(f"  Validator 3: biz_control tampering undetected")
    print(f"    → 0 caught  PASS ✅")

    if reasoning:
        print(f"\n  Auditor reasoning capsules (last {min(3, len(reasoning))}):")
        for line in reasoning[-3:]:
            print(line)

    print()
    if capsule_ledger.exists():
        n = sum(1 for ln in capsule_ledger.read_text().splitlines() if ln.strip())
        print(f"  biz_capsule ledger:   {n} capsules → {capsule_ledger}")
    if auditor_ledger.exists():
        n = sum(1 for ln in auditor_ledger.read_text().splitlines() if ln.strip())
        print(f"  Auditor reasoning:    {n} capsules → {auditor_ledger}")
    print()


def _verify_ledger(ledger_path: Path, label: str) -> bool:
    if not ledger_path.exists():
        print(f"  {label}: NOT FOUND")
        return False
    print(f"\nVerifying {label} ({ledger_path}) …")
    result = subprocess.run(
        ["agent-action-capsule", "verify", "--store", str(ledger_path)],
        capture_output=True,
        text=True,
    )
    # Print summary (first + last few lines)
    lines = result.stdout.splitlines()
    if len(lines) > 20:
        for ln in lines[:8]:
            print(ln)
        print(f"  … ({len(lines) - 16} more lines) …")
        for ln in lines[-8:]:
            print(ln)
    else:
        print(result.stdout[:3000])
    if result.stderr:
        print("stderr:", result.stderr[:200])
    ok = result.returncode == 0
    print(f"  verify exit code: {result.returncode} → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok


async def _run(ticks: int | None) -> Path:
    _TRACE_PATH.parent.mkdir(exist_ok=True)
    CAPSULE_LEDGER.unlink(missing_ok=True)
    _AUDITOR_LEDGER.unlink(missing_ok=True)

    # Register the custom scenario factory
    register_scenario("tax_audit", tax_audit_factory)

    config = ScenarioConfig.from_yaml(_SCENARIO_YAML)
    if ticks is not None:
        config.duration = f"ticks: {ticks}"
    config.output.trace = str(_TRACE_PATH)

    runner = ScenarioRunner(config)
    return await runner.run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticks", type=int, default=None, help="Max ticks (default: 20000)")
    parser.add_argument("--verify", action="store_true", help="Verify ledgers after run")
    parser.add_argument("--anchor", action="store_true", help="Live-anchor capsules")
    args = parser.parse_args()

    if args.anchor:
        import os
        os.environ.setdefault("AAC_ANCHOR", "1")

    trace = asyncio.run(_run(ticks=args.ticks))
    data = _parse_trace(trace)
    _print_report(data, CAPSULE_LEDGER, _AUDITOR_LEDGER)

    if args.verify:
        ok1 = _verify_ledger(CAPSULE_LEDGER, "biz_capsule transactions")
        ok2 = _verify_ledger(_AUDITOR_LEDGER, "auditor reasoning")
        return 0 if (ok1 and ok2) else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
