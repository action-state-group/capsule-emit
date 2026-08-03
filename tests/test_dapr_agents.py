# SPDX-License-Identifier: Apache-2.0
"""Dapr Agents adapter tests — synthetic stub harness (no live sidecar).

Exercises the full adapter surface and round-trips every capsule through the
reference verifier:
- @emitter.tool() → fyi capsule, verifies (sync and async)
- emitter.record_hitl() accept → decide capsule (executed), verifies
- emitter.record_hitl() reject → decide capsule (blocked), verifies
- Fabricated decision rejected at the call site (ValueError)
- dapr_agents extension committed to compute_attestation
- runtime="dapr_agents" in every capsule's compute_attestation
- Constructor-level agent_name / app_id / workflow_instance_id defaults
- Per-decoration / per-call overrides for extension fields
- Chaining: fyi capsule_id threaded as prior_capsule_id into decide capsule
- Emit error in @emitter.tool() warns but does not crash the tool call
- Human approver_id stored in dapr_agents extension, not fabricated
"""
from __future__ import annotations

import asyncio
import warnings

import pytest
from agent_action_capsule import verify

from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emitter(tmp_path, **kw) -> DaprAgentsCapsuleEmitter:
    kw.setdefault("agent_name", "invoice-checker")
    kw.setdefault("app_id", "invoice-app")
    kw.setdefault("workflow_instance_id", "wf-test-001")
    return DaprAgentsCapsuleEmitter(
        operator="acme-co",
        developer="invoice-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ca(emitter: DaprAgentsCapsuleEmitter) -> dict:
    return emitter.last.capsule["model_attestation"]["compute_attestation"]


# ---------------------------------------------------------------------------
# 1.  @emitter.tool() — fyi capsule
# ---------------------------------------------------------------------------

def test_tool_sync_emits_fyi_and_verifies(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("check_invoice")
    def check_invoice(invoice_id: str, amount: str) -> dict:
        return {"status": "ok", "flagged": False}

    result = check_invoice(invoice_id="INV-001", amount="1240.00")
    assert result == {"status": "ok", "flagged": False}

    cap = e.last.capsule
    assert cap["action_type"] == "fyi"
    assert cap["disposition"]["verdict_class"] == "executed"
    assert verify(cap).ok


def test_tool_async_emits_fyi_and_verifies(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("async_lookup")
    async def async_lookup(vendor: str) -> dict:
        return {"vendor": vendor, "tier": "gold"}

    asyncio.run(async_lookup(vendor="Frobozz Supply"))

    cap = e.last.capsule
    assert cap["action_type"] == "fyi"
    assert cap["disposition"]["verdict_class"] == "executed"
    assert verify(cap).ok


def test_tool_action_defaults_to_fn_name(tmp_path):
    e = _emitter(tmp_path)

    @e.tool()
    def fetch_price(item: str) -> dict:
        return {"price": "42.00"}

    fetch_price(item="widget")
    # tool name embedded in action_id as "{tool_name}/{uuid}"
    assert e.last.capsule["action_id"].startswith("fetch_price/")


def test_tool_returns_value_unchanged(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("read_doc")
    def read_doc(doc_id: str) -> str:
        return "content"

    assert read_doc(doc_id="d1") == "content"


# ---------------------------------------------------------------------------
# 2.  runtime and dapr_agents extension
# ---------------------------------------------------------------------------

def test_runtime_is_dapr_agents_in_tool_capsule(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("do_thing")
    def do_thing() -> None:
        return None

    do_thing()
    assert _ca(e).get("runtime") == "dapr_agents"


def test_dapr_ext_populated_from_constructor(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("do_thing")
    def do_thing() -> None:
        return None

    do_thing()
    ext = _ca(e)["dapr_agents"]
    assert ext["agent_name"] == "invoice-checker"
    assert ext["app_id"] == "invoice-app"
    assert ext["workflow_instance_id"] == "wf-test-001"
    assert ext["tool_name"] == "do_thing"


def test_dapr_ext_per_decoration_override(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("do_thing", agent_name="override-agent", app_id="override-app")
    def do_thing() -> None:
        return None

    do_thing()
    ext = _ca(e)["dapr_agents"]
    assert ext["agent_name"] == "override-agent"
    assert ext["app_id"] == "override-app"


def test_dapr_ext_values_are_strings(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("do_thing")
    def do_thing() -> None:
        return None

    do_thing()
    ext = _ca(e)["dapr_agents"]
    for v in ext.values():
        assert isinstance(v, str), f"Expected str, got {type(v)} for {v!r}"


# ---------------------------------------------------------------------------
# 3.  record_hitl — decide capsule, accept
# ---------------------------------------------------------------------------

def test_record_hitl_accept_emits_decide_executed(tmp_path):
    e = _emitter(tmp_path)
    r = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",
        tool_request={"invoice_id": "INV-001", "amount": "1240.00"},
        outcome={"approved_at": "2026-07-28T10:00:00Z"},
        workflow_instance_id="wf-abc123",
    )

    cap = r.capsule
    assert cap["action_type"] == "decide"
    assert cap["disposition"]["verdict_class"] == "executed"
    assert cap["disposition"]["human_disposed"] is True
    assert cap["disposition"]["approver"] == "human"
    assert cap["disposition"]["decision"] == "accept"
    assert verify(cap).ok


def test_record_hitl_accept_approver_id_in_ext(tmp_path):
    e = _emitter(tmp_path)
    r = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",
    )
    ext = r.capsule["model_attestation"]["compute_attestation"]["dapr_agents"]
    assert ext["approver_id"] == "alice@example.com"


def test_record_hitl_runtime_is_dapr_agents(tmp_path):
    e = _emitter(tmp_path)
    r = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",
    )
    assert r.capsule["model_attestation"]["compute_attestation"]["runtime"] == "dapr_agents"


# ---------------------------------------------------------------------------
# 4.  record_hitl — decide capsule, reject
# ---------------------------------------------------------------------------

def test_record_hitl_reject_emits_decide_blocked(tmp_path):
    e = _emitter(tmp_path)
    r = e.record_hitl(
        "approve_payment",
        approver_id="bob@example.com",
        decision="reject",
        tool_request={"invoice_id": "INV-002", "amount": "50000.00"},
        outcome={"rejected_reason": "amount_too_large"},
    )

    cap = r.capsule
    assert cap["action_type"] == "decide"
    assert cap["disposition"]["verdict_class"] == "blocked"
    assert cap["disposition"]["human_disposed"] is True
    assert cap["disposition"]["decision"] == "reject"
    assert verify(cap).ok


def test_record_hitl_reject_approver_id_in_ext(tmp_path):
    e = _emitter(tmp_path)
    r = e.record_hitl(
        "approve_payment",
        approver_id="bob@example.com",
        decision="reject",
    )
    ext = r.capsule["model_attestation"]["compute_attestation"]["dapr_agents"]
    assert ext["approver_id"] == "bob@example.com"


# ---------------------------------------------------------------------------
# 5.  Fabricated disposition guard
# ---------------------------------------------------------------------------

def test_record_hitl_invalid_decision_raises(tmp_path):
    e = _emitter(tmp_path)
    with pytest.raises(ValueError, match="accept.*reject"):
        e.record_hitl(
            "approve_payment",
            approver_id="alice@example.com",
            decision="maybe",
        )


def test_record_hitl_fabricated_decision_raises(tmp_path):
    e = _emitter(tmp_path)
    with pytest.raises(ValueError):
        e.record_hitl(
            "approve_payment",
            approver_id="nobody@example.com",
            decision="approved",
        )


# ---------------------------------------------------------------------------
# 6.  Chaining: fyi → decide via prior_capsule_id
# ---------------------------------------------------------------------------

def test_hitl_chains_to_prior_tool_capsule(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("check_invoice")
    def check_invoice(invoice_id: str) -> dict:
        return {"risk": "low"}

    check_invoice(invoice_id="INV-003")
    fyi_id = e.last.capsule_id

    decide = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",
        prior_capsule_id=fyi_id,
    )

    cap = decide.capsule
    # chain field carries the parent capsule reference
    assert cap["chain"]["parent_capsule_id"] == fyi_id
    assert cap["action_type"] == "decide"
    assert verify(cap).ok


def test_tool_chains_to_prior_decide_capsule(tmp_path):
    """A tool() capsule can chain onto a preceding decide capsule (e.g. a
    fyi escalation emitted after a HITL denial) — the chain isn't limited to
    fyi-then-decide; decide-then-fyi must round-trip too."""
    e = _emitter(tmp_path)

    @e.tool("check_invoice")
    def check_invoice(invoice_id: str) -> dict:
        return {"risk": "high"}

    check_invoice(invoice_id="INV-004")
    fyi_id = e.last.capsule_id

    decide = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="reject",
        prior_capsule_id=fyi_id,
    )
    decide_id = decide.capsule_id

    @e.tool("escalate_to_manager", prior_capsule_id=decide_id)
    def escalate_to_manager(invoice_id: str) -> dict:
        return {"escalated": True}

    escalate_to_manager(invoice_id="INV-004")
    escalate_cap = e.last.capsule

    assert escalate_cap["chain"]["parent_capsule_id"] == decide_id
    assert escalate_cap["action_type"] == "fyi"
    assert verify(escalate_cap).ok


# ---------------------------------------------------------------------------
# 7.  Emit error in @emitter.tool() warns but does not crash
# ---------------------------------------------------------------------------

def test_tool_emit_error_warns_does_not_crash(tmp_path, monkeypatch):
    e = _emitter(tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("storage full")

    monkeypatch.setattr(e, "emit_capsule", _boom)

    @e.tool("bad_tool")
    def bad_tool() -> str:
        return "ok"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = bad_tool()

    assert result == "ok"
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


# ---------------------------------------------------------------------------
# 8.  results / last tracking
# ---------------------------------------------------------------------------

def test_results_accumulate(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("tool_a")
    def tool_a() -> None:
        return None

    @e.tool("tool_b")
    def tool_b() -> None:
        return None

    tool_a()
    tool_b()
    assert len(e.results) == 2
    # tool name embedded in action_id as "{tool_name}/{uuid}"
    assert e.last.capsule["action_id"].startswith("tool_b/")


# ---------------------------------------------------------------------------
# 9.  No raw content in capsule (digest-committed only)
# ---------------------------------------------------------------------------

def test_tool_input_not_in_capsule_body(tmp_path):
    e = _emitter(tmp_path)

    @e.tool("sensitive_call")
    def sensitive_call(secret: str) -> dict:
        return {"ok": True}

    sensitive_call(secret="s3cr3t")
    cap_str = str(e.last.capsule)
    assert "s3cr3t" not in cap_str
    assert "agent_input_digest" in str(
        e.last.capsule["model_attestation"]["compute_attestation"]
    )


# ---------------------------------------------------------------------------
# 10.  Per-call workflow_instance_id override on record_hitl
# ---------------------------------------------------------------------------

def test_record_hitl_workflow_id_override(tmp_path):
    e = DaprAgentsCapsuleEmitter(
        operator="acme-co",
        developer="agent@v1",
        agent_name="my-agent",
        workflow_instance_id="default-wf",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )
    r = e.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",
        workflow_instance_id="override-wf",
    )
    ext = r.capsule["model_attestation"]["compute_attestation"]["dapr_agents"]
    assert ext["workflow_instance_id"] == "override-wf"
