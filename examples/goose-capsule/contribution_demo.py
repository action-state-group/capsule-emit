#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Issues-first contribution demo — sealed evidence under goose's Verification stage.

Goose moved contribution to an issues-first lifecycle ("Moving to issues as the
new PRs", 2026-07-30): Inbox → Accepted/design → Ready → In progress →
**Verification** → Done. PRs must implement a Ready issue and "explain how the
issue's verification plan was carried out" — and the Verification stage is a
human confirming the work. That stage currently runs on prose and eyeballs;
this demo shows the substrate: an agent whose work is sealed as it happens, so
Verification checks records instead of claims.

Lifecycle mapping (what this demo simulates):

  Accepted/design   The issue's VERIFICATION PLAN is agreed (here: the PLAN
                    constant — reproduce the bug, apply the fix, show the
                    failing test pass).
  Ready             Implementation may begin. The agent starts a fresh ledger;
                    every capsule carries the issue linkage in its payload.
  In progress       Each tool call the agent makes is sealed on the spot:
                      1. run_repro    (fyi)    — the bug reproduced, output digest sealed
                      2. apply_patch  (decide) — the change applied, diff digest sealed
                      3. run_tests    (decide) — the suite run, results digest sealed
                    Chained: 2 cites 1, 3 cites 2 — the order of work is part
                    of the record.
  Verification      `capsule-emit evidence --ledger … --issue <url>` renders
                    the evidence comment (see evidence/verification-comment.md
                    for this exact run's output). The reviewer re-verifies
                    offline; the fail-closed builder refuses to render a bundle
                    from records that don't verify.
  Done              The issue closes with the evidence bundle in its record.

The issue below is SYNTHETIC (this repo's own #0 placeholder) — the point is
the shape, not the target. Offline by default: the ledger is local, no anchor
call is made (pass --anchor to submit digests to the live anchor, same as
demo.py; the evidence bundle honestly reports either state).

Run:
    pip install "capsule-emit[dev]"
    python examples/goose-capsule/contribution_demo.py            # offline
    python examples/goose-capsule/contribution_demo.py --anchor   # live anchor
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

from capsule_emit.verification import verify_capsule as verify

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.evidence import build_evidence_markdown

# Synthetic Ready issue — the shape is the point, not the target.
ISSUE_URL = "https://github.com/action-state-group/capsule-emit/issues/0"

# The verification plan as agreed at Accepted/design (goose CONTRIBUTING:
# "working out the design, constraints, and verification plan").
PLAN = [
    "reproduce the reported failure and seal the reproduction output",
    "apply the agreed fix and seal the exact diff",
    "run the test suite and seal the results — the failing test now passes",
]

_ANCHOR = "--anchor" in sys.argv


def _section(title: str) -> None:
    print(f"\n─── {title} " + "─" * max(0, 66 - len(title)))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "contribution-ledger.jsonl"

        emitter = MCPCapsuleEmitter(
            operator="example-org",
            developer="goose-agent@v1",
            ledger=ledger,
            anchor=_ANCHOR,
        )

        # ── In progress: the agent's work, sealed as it happens ──────────
        # Payloads carry the issue linkage so every record is issue-linked
        # the way the policy requires PRs to be, and each capsule cites the
        # one before it (relation="sequence") so the ORDER of work is part
        # of the record — repro before patch before tests, provably.

        _section("Ready → In progress: three sealed, chained tool calls")

        repro_output = "AssertionError: expected 3 rows, got 2  (tests/test_ledger.py:41)"
        r1 = emitter.emit_capsule(
            "run_repro",
            tool_input={"issue": ISSUE_URL},
            tool_output={
                "exit_code": 1,
                "output_sha256": hashlib.sha256(repro_output.encode()).hexdigest(),
            },
            action_type="fyi",
            effect={"status": "dispatched", "type": "reproduce"},
        )

        diff = "--- a/capsule_emit/ledger.py\n+++ b/capsule_emit/ledger.py\n@@ -30 +30 @@ ..."
        r2 = emitter.emit_capsule(
            "apply_patch",
            tool_input={"issue": ISSUE_URL, "path": "capsule_emit/ledger.py"},
            tool_output={"diff_sha256": hashlib.sha256(diff.encode()).hexdigest()},
            action_type="decide",
            effect={"status": "dispatched", "type": "patch"},
            prior_capsule_id=r1.capsule_id,
            relation="sequence",
        )

        results = {"passed": 462, "failed": 0, "previously_failing_now_passing": 1}
        emitter.emit_capsule(
            "run_tests",
            tool_input={"issue": ISSUE_URL},
            tool_output={
                **results,
                "results_sha256": hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest(),
            },
            action_type="decide",
            effect={"status": "dispatched", "type": "test_run"},
            prior_capsule_id=r2.capsule_id,
            relation="sequence",
        )

        capsules = read_ledger(ledger)
        for cap in capsules:
            vr = verify(cap)
            assert vr.ok, f"capsule failed verify: {cap['capsule_id']}"
            print(
                f"  sealed  {cap['action_id'].split('/', 1)[0]:<12}"
                f" verdict={cap['disposition']['verdict_class']:<9}"
                f" capsule_id={cap['capsule_id'][:12]}…  verify=ok"
            )

        # ── Verification: the evidence comment, generated from the ledger ─
        _section("Verification: the evidence comment (fail-closed)")
        markdown = build_evidence_markdown(
            capsules,
            issue_url=ISSUE_URL,
            ledger_name=ledger.name,
        )
        print(markdown)

        # ── The fail-closed half: tampered work cannot produce a bundle ──
        _section("Tamper check: a mutated record refuses to bundle")
        tampered = [json.loads(json.dumps(c)) for c in capsules]
        tampered[1]["model_attestation"]["compute_attestation"]["agent_output_digest"] = "0" * 64
        try:
            build_evidence_markdown(tampered, issue_url=ISSUE_URL)
            print("  ERROR: tampered ledger produced a bundle")
            return 1
        except Exception as exc:
            print(f"  refused, as designed: {str(exc)[:100]}…")

        print("\nverification plan (as agreed at Accepted/design):")
        for i, step in enumerate(PLAN, start=1):
            print(f"  {i}. {step}")
        print(
            "\nEach plan step above maps to the same-numbered sealed capsule in the "
            "table — the reviewer\nchecks the records, not the prose."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
