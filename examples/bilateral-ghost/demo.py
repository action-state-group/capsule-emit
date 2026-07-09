#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bilateral ghost demo — three arcs: authorized, blocked, ghost.

Three cases in one runnable file:

  Arc 1 — Authorized (simple story):
    AAuth CAN grant → Planner seals (may) → DJ seals (did) → both verify.
    Both orgs independently attest over the SAME shared action digest.

  Arc 2 — Out-of-grant BLOCKED:
    Budget cap exceeded → DJ gate fires → DJ seals blocked → verifiable.

  Arc 3 — GHOST (countersign_refused):
    Planner seals request → DJ receives, then goes dark →
    Planner seals countersign_refused (provable asymmetry).

    A ghost is NOT a both-assert:
      · Planner holds 2 capsules: request + ghost
      · DJ holds 0 capsules for this action
      · A verifier sees: Planner committed; counterparty absent.
    The refusal and the ghost are as provable as the performance.

This demo implements the GHOST/countersign_refused asymmetry mechanism from
draft-mih-agent-bilateral-attestation-01 (datatracker window opens Jul 18).

Run (offline — no anchor submission):
    AAC_ANCHOR_URL=off python examples/bilateral-ghost/demo.py

Run (online — anchors to https://anchor.agentactioncapsule.org):
    python examples/bilateral-ghost/demo.py

After running, verify Arc 3:
    agent-action-capsule verify --store /tmp/bilateral_ghost_arc3.jsonl
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import uuid
from pathlib import Path

from agent_action_capsule import verify
from agent_action_capsule.anchor import anchor as _anchor
from agent_action_capsule.canonical import json_digest
from agent_action_capsule.contracts import Disposition, EffectRecord
from agent_action_capsule.emit import emit as _aac_emit

from capsule_emit import read_ledger
from capsule_emit.bilateral import seal_ghost
from capsule_emit.gate import run_gate
from capsule_emit.ledger import append_to_ledger

_SEP = "=" * 64


def _banner(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _ok(msg: str) -> None:
    print(f"  ok  {msg}")


def _warn(msg: str) -> None:
    print(f"  !!  {msg}")


# ---------------------------------------------------------------------------
# AAuth seam (STUB)
# ---------------------------------------------------------------------------

def _stub_aauth_grant() -> str:
    """Return a mock AAuth auth-token JTI (STUB — clearly labeled).

    Live bind point: jti claim extracted from the aa-auth+jwt received after
    the resource_token exchange at the Person Server token endpoint.
    Only the JTI enters the capsule — never the token body.
    """
    return f"aauth-grant-jti:stub-{uuid.uuid4().hex[:16]}"


def _stub_aauth_grant_with_terms() -> dict:
    """Stub grant carrying explicit authorization terms (STUB)."""
    return {
        "jti": f"aauth-grant-jti:stub-{uuid.uuid4().hex[:16]}",
        "scope": ["book_dj_slot"],
        "max_budget_eur": 500,
    }


# ---------------------------------------------------------------------------
# Wicket constraint: BudgetCapConstraint
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class BudgetCapConstraint:
    """Gate constraint: action total must not exceed the grant's budget cap."""

    max_eur: int

    @property
    def name(self) -> str:
        return "budget_cap_eur"

    def check(self, inputs: dict, output: object) -> tuple[bool, str | None]:
        total = inputs.get("total_eur", 0)
        passed = total <= self.max_eur
        reason = (
            None
            if passed
            else f"total_eur {total} exceeds grant cap {self.max_eur}"
        )
        return passed, reason


# ---------------------------------------------------------------------------
# Shared action objects
# ---------------------------------------------------------------------------

ACTION: dict = {
    "type": "book_dj_slot",
    "venue": "Museumsquartier Vienna",
    "date": "2026-07-20",
    "set_duration_min": 90,
    "requester_id": "aauth:planner-v1@planner-org.example",
    "recipient_id": "aauth:dj-v1@dj-org.example",
}

ACTION_OVER_LIMIT: dict = {
    **ACTION,
    "total_eur": 1_200,   # intentionally exceeds max_budget_eur=500
}


# ---------------------------------------------------------------------------
# Anchor helper — respects AAC_ANCHOR_URL=off
# ---------------------------------------------------------------------------

def _resolve_anchor() -> tuple[bool, str | None]:
    raw = os.environ.get("AAC_ANCHOR_URL", "").strip()
    if raw.lower() == "off":
        return False, None
    return True, raw if raw else None


def _fire_anchor(capsule_id: str, endpoint: str | None) -> None:
    kwargs: dict = {}
    if endpoint:
        kwargs["endpoint"] = endpoint
    _anchor(capsule_id, **kwargs)


# ---------------------------------------------------------------------------
# Planner Agent (Org A) seals the request side
# ---------------------------------------------------------------------------

def seal_planner(
    action: dict,
    subject_digest: str,
    grant_jti: str,
    ledger: Path,
    should_anchor: bool,
    anchor_endpoint: str | None,
) -> dict:
    """Planner seals its request attestation.

    disposition.authority carries the AAuth grant JTI as an OPAQUE reference.
    subject_digest is the shared binding — both orgs attest over the same value.
    """
    dispo = Disposition(
        decision="accept",
        approver="policy",
        verdict_class="executed",
        authority=grant_jti,   # opaque AAuth grant ref — never the token body
    )
    effect = EffectRecord(type="book_dj_slot", status="dispatched")
    capsule = _aac_emit(
        action_type="decide",
        operator="planner-org",
        developer="planner-agent@v1",
        tool_name="book_dj_slot",
        compute_attestation={
            "subject_digest": subject_digest,
            "role": "requester",
        },
        effect=effect,
        disposition=dispo,
    )
    append_to_ledger(capsule, ledger)
    if should_anchor:
        _fire_anchor(capsule["capsule_id"], anchor_endpoint)
    return capsule


# ---------------------------------------------------------------------------
# DJ Agent (Org B) seals the action side (within-grant)
# ---------------------------------------------------------------------------

def seal_dj(
    action: dict,
    subject_digest: str,
    planner_capsule_id: str,
    outcome: dict,
    ledger: Path,
    should_anchor: bool,
    anchor_endpoint: str | None,
) -> dict:
    """DJ seals its action attestation (Arc 1: within-grant, confirmed)."""
    response_digest = json_digest(outcome)
    dispo = Disposition(
        decision="accept",
        approver="policy",
        verdict_class="executed",
    )
    effect = EffectRecord(
        type="book_dj_slot",
        status="confirmed",
        response_digest=response_digest,
    )
    capsule = _aac_emit(
        action_type="decide",
        operator="dj-org",
        developer="dj-agent@v1",
        tool_name="book_dj_slot",
        compute_attestation={
            "subject_digest": subject_digest,
            "role": "recipient",
        },
        effect=effect,
        prior_capsule_id=planner_capsule_id,
        chain_relation="confirms",
        disposition=dispo,
    )
    append_to_ledger(capsule, ledger)
    if should_anchor:
        _fire_anchor(capsule["capsule_id"], anchor_endpoint)
    return capsule


# ---------------------------------------------------------------------------
# DJ Agent (Org B) seals BLOCKED (Arc 2: budget cap exceeded)
# ---------------------------------------------------------------------------

def seal_dj_blocked(
    action: dict,
    subject_digest: str,
    grant: dict,
    ledger: Path,
    should_anchor: bool,
    anchor_endpoint: str | None,
) -> dict:
    """DJ seals a blocked capsule when the action exceeds grant terms."""
    constraints = [BudgetCapConstraint(max_eur=grant["max_budget_eur"])]
    gate_result = run_gate(constraints, action, None)

    gate_checks = [
        {"name": r.name, "passed": r.passed, "reason": r.reason}
        for r in gate_result.results
    ]
    dispo = Disposition(
        decision="deny",
        approver="policy",
        verdict_class="blocked",
        authority=grant["jti"],
    )
    effect = EffectRecord(type="book_dj_slot", status="planned")
    capsule = _aac_emit(
        action_type="decide",
        operator="dj-org",
        developer="dj-agent@v1",
        tool_name="book_dj_slot",
        compute_attestation={
            "subject_digest": subject_digest,
            "role": "recipient",
            "gate_checks": gate_checks,
        },
        effect=effect,
        disposition=dispo,
    )
    append_to_ledger(capsule, ledger)
    if should_anchor:
        _fire_anchor(capsule["capsule_id"], anchor_endpoint)
    return capsule


# ---------------------------------------------------------------------------
# Verify helper
# ---------------------------------------------------------------------------

def _verify_ledger(ledger: Path) -> bool:
    records = read_ledger(ledger)
    all_ok = True
    for r in records:
        vr = verify(r)
        cid = r.get("capsule_id", "?")[:20]
        org = r.get("operator", "?")
        vc = r.get("disposition", {}).get("verdict_class", "?")
        compute = (r.get("model_attestation") or {}).get("compute_attestation", {})
        role = compute.get("role", "?")
        asym = compute.get("asymmetry")
        status_str = "ok=True" if vr.ok else f"ok=False  findings={[f.detail for f in vr.findings]}"
        suffix = f"  asymmetry={asym}" if asym else ""
        print(f"  [{org}/{role}] {cid}...  verdict={vc}  {status_str}{suffix}")
        if not vr.ok:
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())
    arc1_ledger = tmpdir / "arc1_authorized.jsonl"
    arc2_ledger = tmpdir / "arc2_blocked.jsonl"
    arc3_ledger = tmpdir / "arc3_ghost.jsonl"

    should_anchor, anchor_endpoint = _resolve_anchor()

    _banner("Bilateral ghost demo — three arcs")
    print(f"  Anchor: {'off (AAC_ANCHOR_URL=off)' if not should_anchor else anchor_endpoint or 'default (anchor.agentactioncapsule.org)'}")
    print("  AAuth:  STUB — grant JTI simulated (real bind point documented in README)")

    # =========================================================================
    # Arc 1 — Authorized (simple story)
    # =========================================================================
    _banner("Arc 1 — Authorized (simple story)")
    print("  CAN grant → Planner seals (may) → DJ seals (did) → verify")
    print(f"  Ledger: {arc1_ledger}")

    print("\n[step 1] Compute shared action digest")
    subject_digest = json_digest(ACTION)
    _ok(f"action: {ACTION['type']} @ {ACTION['venue']} on {ACTION['date']}")
    _ok(f"subject_digest: {subject_digest[:20]}...")

    print("\n[step 2] AAuth grant (STUB — simulates the 'may')")
    grant_jti = _stub_aauth_grant()
    _ok(f"mock grant_jti: {grant_jti}")

    print("\n[step 3] Planner (Org A) seals request — the 'may'")
    cap_a = seal_planner(ACTION, subject_digest, grant_jti, arc1_ledger, should_anchor, anchor_endpoint)
    _ok(f"planner capsule_id:     {cap_a['capsule_id'][:20]}...")
    _ok(f"verdict:                {cap_a['disposition']['verdict_class']}")
    _ok(f"disposition.authority:  {cap_a['disposition']['authority']}")
    _ok(f"effect.status:          {cap_a['effect']['status']}")

    print("\n[step 4] DJ (Org B) seals — the 'did' (confirms)")
    outcome = {
        "accepted": True,
        "slot_confirmed": f"{ACTION['date']} @ {ACTION['venue']}",
        "set_duration_min": ACTION["set_duration_min"],
        "booking_ref": f"DJ-{uuid.uuid4().hex[:8].upper()}",
    }
    cap_b = seal_dj(ACTION, subject_digest, cap_a["capsule_id"], outcome, arc1_ledger, should_anchor, anchor_endpoint)
    _ok(f"dj capsule_id:          {cap_b['capsule_id'][:20]}...")
    _ok(f"verdict:                {cap_b['disposition']['verdict_class']}")
    _ok(f"effect.status:          {cap_b['effect']['status']}")
    _ok(f"chain.parent:           {cap_b['chain']['parent_capsule_id'][:20]}...")

    print("\n[step 5] Shared digest — both orgs attest over the same action")
    sd_a = cap_a["model_attestation"]["compute_attestation"]["subject_digest"]
    sd_b = cap_b["model_attestation"]["compute_attestation"]["subject_digest"]
    if sd_a == sd_b == subject_digest:
        _ok(f"subject_digest matches across both orgs: {subject_digest[:20]}...")
    else:
        _warn("MISMATCH — subject_digest differs!")
        return 1

    print("\n[step 6] Class-1 verify — both capsules")
    if not _verify_ledger(arc1_ledger):
        _warn("Arc 1 verification FAILED")
        return 1
    _ok("Arc 1 complete: both-seal, shared digest, bilateral verifiable")

    # =========================================================================
    # Arc 2 — Out-of-grant BLOCKED
    # =========================================================================
    _banner("Arc 2 — Out-of-grant BLOCKED (budget cap exceeded)")
    print(f"  Ledger: {arc2_ledger}")

    grant_with_terms = _stub_aauth_grant_with_terms()
    subject_digest_over = json_digest(ACTION_OVER_LIMIT)

    print(f"\n[step 1] Action total_eur={ACTION_OVER_LIMIT['total_eur']}  grant cap={grant_with_terms['max_budget_eur']}")
    print("[step 2] DJ runs gate check → budget_cap_eur fires → FAIL")
    print("[step 3] DJ seals BLOCKED capsule")

    cap_blocked = seal_dj_blocked(
        action=ACTION_OVER_LIMIT,
        subject_digest=subject_digest_over,
        grant=grant_with_terms,
        ledger=arc2_ledger,
        should_anchor=should_anchor,
        anchor_endpoint=anchor_endpoint,
    )
    gate_checks = cap_blocked["model_attestation"]["compute_attestation"]["gate_checks"]
    _ok(f"blocked capsule_id: {cap_blocked['capsule_id'][:20]}...")
    _ok(f"verdict:            {cap_blocked['disposition']['verdict_class']}")
    _ok(f"gate_checks:        {gate_checks}")

    print("\n[step 4] Class-1 verify")
    if not _verify_ledger(arc2_ledger):
        _warn("Arc 2 verification FAILED")
        return 1
    _ok("Arc 2 complete: blocked sealed, gate checks recorded, verifiable")

    # =========================================================================
    # Arc 3 — GHOST (countersign_refused)
    # =========================================================================
    _banner("Arc 3 — GHOST (countersign_refused per bilateral -01)")
    print("  Planner seals request → DJ goes dark → Planner seals asymmetry")
    print(f"  Ledger: {arc3_ledger}")

    print("\n[step 1] Compute action digest (same action type as Arc 1)")
    subject_digest_ghost = json_digest(ACTION)
    _ok(f"subject_digest: {subject_digest_ghost[:20]}...")

    print("\n[step 2] AAuth grant (STUB)")
    grant_jti_ghost = _stub_aauth_grant()
    _ok(f"mock grant_jti: {grant_jti_ghost}")

    print("\n[step 3] Planner seals request — committed, awaiting DJ")
    cap_request = seal_planner(
        ACTION, subject_digest_ghost, grant_jti_ghost,
        arc3_ledger, should_anchor, anchor_endpoint,
    )
    _ok(f"request capsule_id:     {cap_request['capsule_id'][:20]}...")
    _ok(f"verdict:                {cap_request['disposition']['verdict_class']}")
    _ok(f"effect.status:          {cap_request['effect']['status']}")

    print("\n[step 4] DJ receives request — and goes dark (no capsule sealed)")
    print("         DJ's record for this action: [EMPTY — 0 capsules]")
    print("         (In a real deployment: request delivered, retry window elapsed,")
    print("          no response, no countersignature.)")

    print("\n[step 5] Planner seals countersign_refused — the provable asymmetry")
    print("         verdict_class='countersign_refused'  effect.status='planned'")
    print("         chain.relation='supersedes'  chain.parent → request capsule")
    ghost_result = seal_ghost(
        requester_org="planner-org",
        developer="planner-agent@v1",
        action="book_dj_slot",
        action_digest=subject_digest_ghost,
        request_capsule_id=cap_request["capsule_id"],
        ledger=str(arc3_ledger),
        anchor=should_anchor,
    )
    ghost_capsule = ghost_result.capsule
    _ok(f"ghost capsule_id:       {ghost_result.capsule_id[:20]}...")
    _ok(f"verdict:                {ghost_capsule['disposition']['verdict_class']}")
    _ok(f"effect.status:          {ghost_capsule['effect']['status']}")
    _ok(f"chain.relation:         {ghost_capsule['chain']['relation']}")
    _ok(f"chain.parent:           {ghost_capsule['chain']['parent_capsule_id'][:20]}...")
    _ok(f"chain.parent == request_capsule_id: {ghost_capsule['chain']['parent_capsule_id'] == cap_request['capsule_id']}")

    print("\n[step 6] Verify the asymmetry")
    arc3_records = read_ledger(arc3_ledger)
    planner_capsules = [r for r in arc3_records if r.get("operator") == "planner-org"]
    dj_capsules = [r for r in arc3_records if r.get("operator") == "dj-org"]
    _ok(f"planner-org holds {len(planner_capsules)} capsule(s): request + ghost")
    _ok(f"dj-org holds      {len(dj_capsules)} capsule(s): [absent]")

    if len(planner_capsules) != 2:
        _warn(f"Expected 2 planner capsules, got {len(planner_capsules)}")
        return 1
    if len(dj_capsules) != 0:
        _warn(f"Expected 0 dj capsules, got {len(dj_capsules)}")
        return 1

    print("\n  Class-1 verify — both Planner capsules")
    if not _verify_ledger(arc3_ledger):
        _warn("Arc 3 verification FAILED")
        return 1

    print("\n  Asymmetry proof:")
    print(f"    request    capsule: {cap_request['capsule_id'][:20]}...  verdict=executed  effect=dispatched")
    print(f"    ghost      capsule: {ghost_result.capsule_id[:20]}...  verdict=countersign_refused  effect=planned")
    print(f"    DJ               : [0 capsules — no countersignature]")
    print()
    print("  A verifier trusting neither org can confirm:")
    print("    · Planner committed (request sealed, AAuth grant ref in disposition.authority)")
    print("    · Counterparty countersignature: absent")
    print("    · This is NOT a symmetric both-assert — the asymmetry IS the evidence")
    print("    · agent-action-capsule verify --store", arc3_ledger)

    _ok("Arc 3 complete: ghost/countersign_refused sealed, asymmetry provable")

    # =========================================================================
    # Summary
    # =========================================================================
    _banner("Summary — all three arcs")
    print(f"  Arc 1 — Authorized   : {arc1_ledger}")
    print(f"  Arc 2 — Blocked      : {arc2_ledger}")
    print(f"  Arc 3 — Ghost        : {arc3_ledger}")
    print()
    print("  All arcs independently verifiable:")
    print("    agent-action-capsule verify --store <ledger>")
    print()
    print("  Spec reference: draft-mih-agent-bilateral-attestation-01")
    print("    GHOST = countersign_refused: the counterparty's absence is evidence,")
    print("    not silence. The honest party's two capsules prove what they committed")
    print("    to and what they never received. Refusal and performance are equally provable.")
    if should_anchor:
        print()
        print("  Anchor: digest-only POSTs submitted (no business content crosses the wire).")
    print(f"\n{_SEP}")
    print("  Demo complete.")
    print(_SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
