#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Startup lifecycle template — a worked example of intension vs. extension.

Run:
    cd examples/startup-lifecycle-template
    python run_example.py

What this shows
----------------
1. ``template.json`` declares a lifecycle template — the INTENSION: an expected
   set of ``action_id``s, an order, and a completeness rule. It names no
   run; it is a reusable, registrable declaration.
2. The template is sealed as its own capsule. Its ``capsule_id`` is a
   ``c_digest`` — a digest that itself has provenance (who declared this
   template, when, sealed and witnessed) and can be cited in the
   ``derivation`` field of a bundle-request (see
   ../../docs/a2a-request-shape.md).
3. A simulated run seals its own member capsules (illustrative — one action
   per member, deliberately incomplete and out of order here to exercise
   the check) — the EXTENSION: what actually happened, cited by
   ``action_id`` in occurrence order.
4. ``completeness.evaluate()`` computes the diff: a missing or out-of-order
   member is always a reported finding, never a silent gap.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT))

from completeness import evaluate  # noqa: E402

from capsule_emit import seal  # noqa: E402

OPERATOR = "action-state-group"
DEVELOPER = "startup-lifecycle-example@v1"
LEDGER_PATH = Path(tempfile.mkdtemp()) / "startup_lifecycle_ledger.jsonl"


def main() -> None:
    template = json.loads((_HERE / "template.json").read_text())

    # 1. Seal the template itself — the intension, declared once. "dispatched"
    #    (not "confirmed"): a template registration has no observed response
    #    to bind a response_digest to — it is a one-shot declaration.
    template_capsule = seal(
        template,
        action="register_lifecycle_template",
        operator=OPERATOR,
        developer=DEVELOPER,
        verdict="executed",
        effect={"type": "register_lifecycle_template", "status": "dispatched"},
        ledger=str(LEDGER_PATH),
    )
    c_digest = template_capsule.capsule_id
    print(f"template sealed — c_digest = {c_digest}")

    # 2. A worked bundle-request that cites this template by c_digest (see
    #    ../../docs/a2a-request-shape.md for the full shape).
    bundle_request = {
        "subject": {"kind": "account", "id": "demo-startup-run-1"},
        "coverage": {"kind": "pin", "digest": c_digest},
        "derivation": {"kind": "c_digest", "c_digest": c_digest},
    }
    print("example bundle-request citing this template as derivation.c_digest:")
    print(json.dumps(bundle_request, indent=2))

    # 3. Simulate a run: seal member capsules — deliberately incomplete
    #    (site.demo_published.walkthrough never happens) and out of order
    #    (the health check runs before deploy is confirmed) to exercise
    #    evaluate()'s findings.
    simulated_order = [
        "infra.dnssec_enabled",
        "infra.tls_cert_issued",
        "infra.dns_propagated",
        "app.health_check_passed",  # out of order — should follow deploy_confirmed
        "app.deploy_confirmed",
        "site.demo_published.draft",
        # site.demo_published.walkthrough intentionally omitted — missing member
    ]
    observed = []
    for action_id in simulated_order:
        member_capsule = seal(
            {"action_id": action_id},
            action=action_id,
            operator=OPERATOR,
            developer=DEVELOPER,
            agent_output={"action_id": action_id, "result": "ok"},
            verdict="executed",
            effect={"type": action_id, "status": "confirmed"},
            ledger=str(LEDGER_PATH),
        )
        observed.append({"action_id": action_id, "capsule_id": member_capsule.capsule_id})

    # 4. Compute the diff.
    report = evaluate(template, observed)
    print(f"\nobserved {len(observed)} of {len(template['members'])} declared members")
    print(f"complete: {report['complete']}")
    for finding in report["findings"]:
        print(f"  finding: {finding}")


if __name__ == "__main__":
    main()
