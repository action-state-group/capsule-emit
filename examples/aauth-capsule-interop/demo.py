#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""AAuth + Capsule bilateral interop demo — Planner Agent (Org A) ↔ DJ Agent (Org B).

Demonstrates:
  1. AAuth = the "may": a cross-org authorization grant captured as an opaque
     reference in disposition.authority. In this demo the grant is a stub (clearly
     labeled); see "What runs live vs. stubbed" in README.md.
  2. Bilateral seal = the "did" (both directions): both orgs' agents independently
     seal a capsule over the SAME shared action digest
     subject_digest = SHA-256(JCS(action)), each bound to its own part.
  3. Anchor (default) → verify: a third party trusting NEITHER org can run
     agent-action-capsule verify and confirm the shared action end-to-end.

Run (online, anchors to https://anchor.agentactioncapsule.org):
    pip install "capsule-emit" "agent-action-capsule"
    python examples/aauth-capsule-interop/demo.py

Run (offline — no anchor submission):
    AAC_ANCHOR_URL=off python examples/aauth-capsule-interop/demo.py

After running, verify:
    agent-action-capsule verify --store /tmp/aauth_capsule_interop_ledger.jsonl

Reputation leg: omitted (not required for the compose story).
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
from capsule_emit.gate import run_gate
from capsule_emit.ledger import append_to_ledger

_SEP = "=" * 64


def _banner(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _ok(msg: str) -> None:
    print(f"  ok  {msg}")


def _info(msg: str) -> None:
    print(f"      {msg}")


def _warn(msg: str) -> None:
    print(f"  !!  {msg}")


# ---------------------------------------------------------------------------
# AAuth seam (STUB — clearly labeled)
# ---------------------------------------------------------------------------

def _stub_aauth_grant() -> str:
    """Return a mock AAuth auth-token JTI representing the cross-org authorization.

    STUB: In a live deployment, this is the `jti` claim extracted from the
    `aa-auth+jwt` auth token received after the planner-agent sends its
    `aa-agent+jwt`, receives a 401 with a resource_token from dj-agent,
    exchanges that resource_token at the Person Server token endpoint (see the
    AAuth SPEC.md token-endpoint / auth-token sections), and the PS issues an
    auth token. The `jti` of that auth token is the
    unique grant identifier. It is stored as an OPAQUE reference — the token
    contents are never placed in the capsule.

    The exact bind point: after `exchange_resource_token()` returns the
    aa-auth+jwt, parse its `jti` claim and pass it here as `authority`.
    """
    return f"aauth-grant-jti:stub-{uuid.uuid4().hex[:16]}"


def _stub_aauth_grant_with_terms() -> dict:
    """Stub grant that carries explicit authorization terms.

    STUB: In a live AAuth deployment the grant terms would be embedded as
    claims in the aa-auth+jwt. Here we use a plain dict to represent the
    authorized scope and budget cap. The jti is the opaque reference
    stored in disposition.authority.
    """
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
    """Wicket constraint: action total must not exceed the grant's budget cap."""

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
# Shared action — the object both parties attest over
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
    "total_eur": 1_200,  # intentionally exceeds max_budget_eur=500
}


# ---------------------------------------------------------------------------
# Anchor helper — respects AAC_ANCHOR_URL=off
# ---------------------------------------------------------------------------

def _resolve_anchor() -> tuple[bool, str | None]:
    """Return (should_anchor, endpoint_or_None).

    AAC_ANCHOR_URL=off  → skip anchoring entirely.
    AAC_ANCHOR_URL unset → use the default endpoint.
    AAC_ANCHOR_URL=<url> → use that URL.
    """
    raw = os.environ.get("AAC_ANCHOR_URL", "").strip()
    if raw.lower() == "off":
        return False, None
    return True, raw if raw else None


def _fire_anchor(capsule_id: str, endpoint: str | None) -> None:
    """Non-blocking digest-only POST to the anchor endpoint."""
    kwargs: dict = {}
    if endpoint:
        kwargs["endpoint"] = endpoint
    _anchor(capsule_id, **kwargs)


# ---------------------------------------------------------------------------
# Planner Agent (Org A) seals
# ---------------------------------------------------------------------------

def seal_planner(
    action: dict,
    subject_digest: str,
    grant_jti: str,
    ledger: Path,
    should_anchor: bool,
    anchor_endpoint: str | None,
) -> dict:
    """Planner Agent (Org A) seals its side of the bilateral attestation.

    disposition.authority carries the AAuth grant JTI as an OPAQUE reference.
    agent_input is the shared action dict; its digest == subject_digest.
    """
    dispo = Disposition(
        decision="accept",
        approver="policy",
        verdict_class="executed",
        # AAuth bind point: opaque reference to the auth-token JTI.
        # Never the token body — only the identifier.
        authority=grant_jti,
    )
    effect = EffectRecord(
        type="book_dj_slot",
        status="dispatched",
    )
    capsule = _aac_emit(
        action_type="decide",
        operator="planner-org",
        developer="planner-agent@v1",
        tool_name="book_dj_slot",
        compute_attestation={
            # shared digest — both parties attest over the same canonical value
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
# DJ Agent (Org B) seals
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
    """DJ Agent (Org B) seals its side of the bilateral attestation.

    Chains onto the Planner's capsule_id. Uses the SAME subject_digest so a
    third party can confirm both parties attested over the same action.
    effect.status='confirmed' with response_digest binds the outcome.
    """
    # response_digest commits the DJ's acceptance outcome
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
            # same subject_digest as Planner — the shared binding
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
# DJ Agent (Org B) seals a BLOCKED capsule (out-of-grant)
# ---------------------------------------------------------------------------

def seal_dj_blocked(
    action: dict,
    subject_digest: str,
    grant: dict,
    ledger: Path,
    should_anchor: bool,
    anchor_endpoint: str | None,
) -> dict:
    """DJ Agent (Org B) seals a BLOCKED capsule when the action exceeds grant terms.

    Runs a BudgetCapConstraint derived from the grant's max_budget_eur.
    On gate failure: sealed as verdict='blocked', effect.status='planned',
    gate_checks recorded, disposition.authority = grant jti (opaque ref).
    """
    constraints = [BudgetCapConstraint(max_eur=grant["max_budget_eur"])]
    gate_result = run_gate(constraints, action, None)
    # gate_result.passed is False — that is the whole point of this case

    gate_checks = [
        {"name": r.name, "passed": r.passed, "reason": r.reason}
        for r in gate_result.results
    ]

    dispo = Disposition(
        decision="deny",
        approver="policy",
        verdict_class="blocked",
        authority=grant["jti"],  # opaque grant ref — not the token body
    )
    effect = EffectRecord(
        type="book_dj_slot",
        status="planned",
    )
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
# Verify pass
# ---------------------------------------------------------------------------

def verify_ledger(ledger: Path) -> bool:
    """Run Class-1 verify on both capsules. Return True if all ok."""
    records = read_ledger(ledger)
    all_ok = True
    for r in records:
        vr = verify(r)
        cid = r.get("capsule_id", "?")[:20]
        org = r.get("operator", "?")
        vc = r.get("disposition", {}).get("verdict_class", "?")
        auth = r.get("disposition", {}).get("authority")
        sd = (r.get("model_attestation") or {}).get("compute_attestation", {}).get("subject_digest", "")
        role = (r.get("model_attestation") or {}).get("compute_attestation", {}).get("role", "?")
        status = "ok=True" if vr.ok else f"ok=False  findings={[f.detail for f in vr.findings]}"
        print(f"  [{org}/{role}] {cid}...  verdict={vc}  {status}")
        if auth:
            print(f"    authority (AAuth grant ref): {auth}")
        if sd:
            print(f"    subject_digest: {sd[:20]}...")
        if not vr.ok:
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ledger = Path(tempfile.mkdtemp()) / "aauth_capsule_interop_ledger.jsonl"
    should_anchor, anchor_endpoint = _resolve_anchor()

    _banner("AAuth + Capsule bilateral interop demo")
    print(f"  Ledger:  {ledger}")
    print(f"  Anchor:  {'off (AAC_ANCHOR_URL=off)' if not should_anchor else anchor_endpoint or 'default (anchor.agentactioncapsule.org)'}")
    print("  AAuth:   STUB — grant JTI simulated (real bind point documented in README)")

    # ── Case 1: Within-grant (executed) ──────────────────────────────────────
    print("\n[Case 1] Within-grant action — budget cap satisfied (executed)")

    # ── Step 1: Compute shared subject_digest ────────────────────────────────
    print("\n[step 1] Compute shared action digest (subject_digest = SHA-256(JCS(action)))")
    subject_digest = json_digest(ACTION)
    _ok(f"action:  {ACTION['type']} @ {ACTION['venue']} on {ACTION['date']}")
    _ok(f"subject_digest: {subject_digest[:20]}...")

    # ── Step 2: AAuth grant (stub) ────────────────────────────────────────────
    print("\n[step 2] AAuth grant — STUB (simulates the may)")
    print("  STUB: In production, planner-agent exchanges a resource_token at")
    print("        the Person Server token endpoint (see AAuth SPEC.md) and receives")
    print("        an aa-auth+jwt. The jti of that token is the grant identifier.")
    grant_jti = _stub_aauth_grant()
    _ok(f"mock grant_jti: {grant_jti}")

    # ── Step 3: Planner Agent seals (Org A) ───────────────────────────────────
    print("\n[step 3] Planner Agent (Org A) seals — the did (requester side)")
    cap_a = seal_planner(ACTION, subject_digest, grant_jti, ledger, should_anchor, anchor_endpoint)
    _ok(f"planner capsule_id:  {cap_a['capsule_id'][:20]}...")
    _ok(f"disposition.verdict: {cap_a['disposition']['verdict_class']}")
    _ok(f"disposition.authority (AAuth grant ref): {cap_a['disposition']['authority']}")
    _ok(f"effect.status: {cap_a['effect']['status']}")

    # ── Step 4: DJ Agent seals (Org B) ────────────────────────────────────────
    print("\n[step 4] DJ Agent (Org B) seals — the did (recipient side, confirms)")
    outcome = {
        "accepted": True,
        "slot_confirmed": f"{ACTION['date']} @ {ACTION['venue']}",
        "set_duration_min": ACTION["set_duration_min"],
        "booking_ref": f"DJ-{uuid.uuid4().hex[:8].upper()}",
    }
    cap_b = seal_dj(ACTION, subject_digest, cap_a["capsule_id"], outcome, ledger, should_anchor, anchor_endpoint)
    _ok(f"dj capsule_id:       {cap_b['capsule_id'][:20]}...")
    _ok(f"disposition.verdict: {cap_b['disposition']['verdict_class']}")
    _ok(f"effect.status:       {cap_b['effect']['status']}")
    _ok(f"chain.parent:        {cap_b['chain']['parent_capsule_id'][:20]}...")

    # ── Step 5: Shared digest check ────────────────────────────────────────────
    print("\n[step 5] Both capsules attest over the SAME subject_digest")
    sd_a = cap_a["model_attestation"]["compute_attestation"]["subject_digest"]
    sd_b = cap_b["model_attestation"]["compute_attestation"]["subject_digest"]
    if sd_a == sd_b == subject_digest:
        _ok(f"subject_digest matches across both orgs: {subject_digest[:20]}...")
    else:
        _warn("MISMATCH — subject_digest differs between orgs!")
        return 1

    # ── Step 6: Verify ────────────────────────────────────────────────────────
    print("\n[step 6] Verify — agent-action-capsule Class-1 verify (both orgs)")
    all_ok = verify_ledger(ledger)

    if all_ok:
        _ok("All capsules ok=True — bilateral interop verified.")
    else:
        _warn("Verification FAILED — see findings above.")
        return 1

    # ── Case 2: Out-of-grant action (budget cap exceeded) ──────────────────
    print("\n[Case 2] Out-of-grant action — budget cap exceeded")
    grant_with_terms = _stub_aauth_grant_with_terms()
    subject_digest_over = json_digest(ACTION_OVER_LIMIT)
    capsule_blocked = seal_dj_blocked(
        action=ACTION_OVER_LIMIT,
        subject_digest=subject_digest_over,
        grant=grant_with_terms,
        ledger=ledger,
        should_anchor=should_anchor,
        anchor_endpoint=anchor_endpoint,
    )
    print(f"  blocked capsule_id : {capsule_blocked['capsule_id']}")
    print(f"  verdict            : {capsule_blocked['disposition']['verdict_class']}")
    blocked_gate_checks = capsule_blocked["model_attestation"]["compute_attestation"]["gate_checks"]
    print(f"  gate_checks        : {blocked_gate_checks}")
    print(f"  grant ref (jti)    : {capsule_blocked['disposition']['authority']}")

    ok_blocked = verify_ledger(ledger)
    print(f"  ledger verify      : ok={ok_blocked}")

    print(
        "\n  Case 1 (executed) and Case 2 (blocked) are both independently verifiable"
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    _banner("Summary")
    print("  AAuth (may):")
    print(f"    grant_jti (stub): {grant_jti}")
    print("    bind point: disposition.authority on the planner's capsule")
    print("    real token: jti from aa-auth+jwt received at the PS token endpoint")
    print()
    print("  Bilateral seal (did, both directions):")
    print(f"    Planner (Org A): {cap_a['capsule_id'][:20]}...")
    print(f"    DJ     (Org B): {cap_b['capsule_id'][:20]}...")
    print(f"    Shared subject_digest: {subject_digest}")
    print()
    print("  Third-party verify:")
    print(f"    agent-action-capsule verify --store {ledger}")
    print()
    print("  Reputation leg: OMITTED (not required for the compose story).")
    print()
    if should_anchor:
        print("  Anchor: fire-and-forget digest-only POST submitted.")
        print("    No business content crosses the wire — only capsule_id hex.")
    else:
        print("  Anchor: skipped (AAC_ANCHOR_URL=off).")

    print(f"\n{_SEP}")
    print("  Demo complete.")
    print(_SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
