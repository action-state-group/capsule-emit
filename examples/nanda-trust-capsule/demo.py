#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Track-A demo: receipt_reputation scenario with CapsuleEmitTrust.

Demonstrates that swapping `trust: capsule_emit` for `trust: agent_receipts`:
  - Produces identical ring-severance results (receipt_reputation validator passes)
  - Anchors every interaction to a capsule ledger verifiable by any third party

Usage:
    python demo.py                    # run with capsule_emit, anchor=False
    python demo.py --anchor           # live-anchor capsules (requires AAC_ANCHOR_URL)
    python demo.py --verify           # run + verify ledger with agent-action-capsule
    python demo.py --ticks 1000       # shorter run (~5s vs 30s)
    python demo.py --compare          # side-by-side vs agent_receipts baseline

The capsule ledger is written to `capsule_ledger.jsonl` in the working directory.
After the run, verify it independently:
    agent-action-capsule verify --store capsule_ledger.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from nest_core.plugins import PluginRegistry
from nest_core.runner import ScenarioRunner
from nest_core.scenario import ScenarioConfig

_SCENARIO_YAML = Path(__file__).parent / "scenarios" / "receipt_reputation_capsule.yaml"
_TRACE_DIR = Path(__file__).parent / "traces"
_CAPSULE_LEDGER = Path("capsule_ledger.jsonl")


def _score_table(trace_path: Path) -> dict[str, tuple[float, str]]:
    """Extract (score, role) per agent from the NANDA trace."""
    scores: dict[str, tuple[float, str]] = {}
    if not trace_path.exists():
        return scores
    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        body = event.get("msg", event.get("body", ""))
        if body.startswith("score:"):
            parts = body.split(":")
            if len(parts) >= 5:
                agent, score_s, _conf, role = parts[1], parts[2], parts[3], parts[4]
                try:
                    scores[agent] = (float(score_s), role)
                except ValueError:
                    pass
    return scores


def _print_summary(scores: dict[str, tuple[float, str]], ledger_path: Path, label: str) -> None:
    print(f"\n{'=' * 62}")
    print(f"  {label}")
    print(f"{'=' * 62}")
    for agent in sorted(scores):
        score, role = scores[agent]
        flag = "✓" if (role == "honest" and score > 0.1) else ("✗" if role == "ring" else " ")
        print(f"  {flag} {agent:<22} role={role:<10} score={score:.4f}")

    ring_scores = [s for s, r in scores.values() if r == "ring"]
    honest_scores = [s for s, r in scores.values() if r == "honest"]
    print()
    if ring_scores:
        print(f"  Ring mean score:   {sum(ring_scores)/len(ring_scores):.4f}  (target: ~0)")
    if honest_scores:
        print(f"  Honest mean score: {sum(honest_scores)/len(honest_scores):.4f}  (target: >0)")

    validator_pass = (
        all(s < 0.05 for s in ring_scores) and
        all(s > 0.1 for s in honest_scores)
    ) if ring_scores and honest_scores else False
    print(f"  receipt_reputation validator: {'PASS ✅' if validator_pass else 'FAIL ❌'}")

    if ledger_path.exists():
        capsule_count = sum(1 for ln in ledger_path.read_text().splitlines() if ln.strip())
        print(f"  Capsule ledger:    {capsule_count} capsules → {ledger_path}")
    print()


def _verify_ledger(ledger_path: Path) -> bool:
    """Run `agent-action-capsule verify --store` on the ledger."""
    print(f"\nVerifying {ledger_path} with agent-action-capsule …")
    result = subprocess.run(
        ["agent-action-capsule", "verify", "--store", str(ledger_path)],
        capture_output=True,
        text=True,
    )
    print(result.stdout[:3000] if result.stdout else "(no stdout)")
    if result.stderr:
        print("stderr:", result.stderr[:500])
    ok = result.returncode == 0
    print(f"  verify exit code: {result.returncode} → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok


async def _run(yaml_path: Path, ticks: int | None, override_trust: str | None = None) -> Path:
    """Load a scenario YAML and run it; return trace path."""
    _TRACE_DIR.mkdir(exist_ok=True)
    config = ScenarioConfig.from_yaml(yaml_path)
    if ticks is not None:
        config.duration = f"ticks: {ticks}"
    if override_trust is not None:
        config.layers.trust = override_trust
    runner = ScenarioRunner(config)
    return await runner.run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--anchor", action="store_true", help="Live-anchor capsules (needs AAC_ANCHOR_URL)")
    parser.add_argument("--verify", action="store_true", help="Verify capsule ledger after run")
    parser.add_argument("--compare", action="store_true", help="Side-by-side vs agent_receipts baseline")
    parser.add_argument("--ticks", type=int, default=None, help="Override tick count (default: 10000)")
    args = parser.parse_args()

    import os
    if args.anchor:
        os.environ.setdefault("AAC_ANCHOR", "1")

    _CAPSULE_LEDGER.unlink(missing_ok=True)

    print("Running receipt_reputation_capsule scenario (trust: capsule_emit) …")
    trace = asyncio.run(_run(_SCENARIO_YAML, ticks=args.ticks))
    scores = _score_table(trace)
    _print_summary(scores, _CAPSULE_LEDGER, "CapsuleEmitTrust — trust: capsule_emit")

    if args.compare:
        print("\nRunning reference scenario (trust: agent_receipts) for comparison …")
        ref_yaml = _SCENARIO_YAML  # same YAML, override trust layer
        ref_trace = asyncio.run(_run(ref_yaml, ticks=args.ticks, override_trust="agent_receipts"))
        ref_scores = _score_table(ref_trace)
        _print_summary(ref_scores, Path("/dev/null"), "AgentReceiptsTrust — trust: agent_receipts (reference)")

    if args.verify:
        if _CAPSULE_LEDGER.exists():
            ok = _verify_ledger(_CAPSULE_LEDGER)
            return 0 if ok else 1
        else:
            print("No capsule ledger produced — nothing to verify.")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
