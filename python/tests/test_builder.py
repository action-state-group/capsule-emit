# SPDX-License-Identifier: Apache-2.0
"""Tests for rung-1 emit() — both the full API and the adapter-tier API.

Full API (action_type="decide", model_id, provider, compute_attestation):
  - ModelAttestation tamper-evidence: capsule_id commits all three model fields.
  - Effect records, chaining, confirmed/dispatched flows.

Adapter-tier API (action_type="fyi", no model_id needed):
  - emit(operator=..., developer=..., tool_name=...) returns a valid capsule.
  - Dispositions, chaining, action_type defaults.
"""
import pytest
from agent_action_capsule import (
    EffectRecord,
    ModelAttestation,
    compute_capsule_id,
    json_digest,
    verify,
)
from agent_action_capsule.contracts import InvariantError
from agent_action_capsule.versions import FORMAT_VERSION, SPEC_VERSION

from capsule_emit.builder import build_capsule as emit

# ---------------------------------------------------------------------------
# Full API tests (action_type="decide", model_id + provider required)
# ---------------------------------------------------------------------------

EMIT_KWARGS = dict(
    action_id="act-001",
    action_type="decide",
    operator="test-org",
    developer="test-agent@1.0",
    model_id="claude-sonnet-4-6",
    provider="anthropic",
    compute_attestation={"endpoint": "https://api.anthropic.com", "chip": "TPU-v5"},
)


def test_emit_returns_sealed_capsule():
    sealed = emit(**EMIT_KWARGS)
    assert isinstance(sealed, dict)
    assert len(sealed["capsule_id"]) == 64
    assert sealed["capsule_id"].islower()


def test_emit_verify_ok():
    sealed = emit(**EMIT_KWARGS)
    result = verify(sealed)
    assert result.ok, [f.detail for f in result.findings if f.severity == "error"]


def test_emit_model_attestation_in_body():
    sealed = emit(**EMIT_KWARGS)
    ma = sealed["model_attestation"]
    assert ma["model_id"] == "claude-sonnet-4-6"
    assert ma["provider"] == "anthropic"
    assert ma["compute_attestation"] == {"endpoint": "https://api.anthropic.com", "chip": "TPU-v5"}


def test_model_attestation_committed_to_capsule_id():
    sealed = emit(**EMIT_KWARGS)
    assert sealed["capsule_id"] == compute_capsule_id({k: v for k, v in sealed.items() if k != "chain"})


def test_tamper_model_id_fails_verify():
    sealed = emit(**EMIT_KWARGS)
    tampered = dict(sealed)
    tampered["model_attestation"] = dict(tampered["model_attestation"])
    tampered["model_attestation"]["model_id"] = "TAMPERED"
    result = verify(tampered)
    assert not result.ok


def test_tamper_provider_fails_verify():
    sealed = emit(**EMIT_KWARGS)
    tampered = dict(sealed)
    tampered["model_attestation"] = dict(tampered["model_attestation"])
    tampered["model_attestation"]["provider"] = "TAMPERED"
    result = verify(tampered)
    assert not result.ok


def test_tamper_compute_attestation_fails_verify():
    sealed = emit(**EMIT_KWARGS)
    tampered = dict(sealed)
    tampered["model_attestation"] = dict(tampered["model_attestation"])
    tampered["model_attestation"]["compute_attestation"] = {"endpoint": "FAKE", "chip": "FAKE"}
    result = verify(tampered)
    assert not result.ok


def test_emit_without_compute_attestation():
    kw = {k: v for k, v in EMIT_KWARGS.items() if k != "compute_attestation"}
    sealed = emit(**kw)
    result = verify(sealed)
    assert result.ok
    assert "compute_attestation" not in sealed.get("model_attestation", {})


def test_emit_dispatched_effect():
    req_digest = json_digest({"request": "do-something"})
    sealed = emit(
        **EMIT_KWARGS,
        effect=EffectRecord(
            status="dispatched",
            type="api_call",
            request_digest=req_digest,
        ),
    )
    result = verify(sealed)
    assert result.ok
    assert result.assurance["effect_mode"] == "dispatched_unconfirmed"


def test_emit_confirmed_effect():
    req_digest = json_digest({"request": "do-something"})
    resp_digest = json_digest({"result": "ok"})
    sealed = emit(
        **EMIT_KWARGS,
        effect=EffectRecord(
            status="confirmed",
            type="api_call",
            request_digest=req_digest,
            response_digest=resp_digest,
        ),
    )
    result = verify(sealed)
    assert result.ok
    assert result.assurance["effect_mode"] == "confirmed"


def test_emit_chained_capsule():
    kw = {k: v for k, v in EMIT_KWARGS.items() if k != "action_id"}
    first = emit(action_id="act-001", **kw)
    second = emit(
        action_id="act-002",
        prior_capsule_id=first["capsule_id"],
        **kw,
    )
    assert second.get("chain", {}).get("parent_capsule_id") == first["capsule_id"]
    result = verify(second)
    assert result.ok
    assert result.assurance["ledger_mode"] == "chained"


def test_emit_dispatched_then_chained_confirmed():
    req_digest = json_digest({"call": "stripe_charge", "amount": "40.00"})
    kw = {k: v for k, v in EMIT_KWARGS.items() if k != "action_id"}

    dispatched = emit(
        action_id="charge-dispatch",
        effect=EffectRecord(
            status="dispatched",
            type="stripe_charge",
            request_digest=req_digest,
        ),
        **kw,
    )
    assert verify(dispatched).ok
    assert dispatched["assurance"]["effect_mode"] == "dispatched_unconfirmed"

    resp_digest = json_digest({"charge_id": "ch_test_123", "status": "succeeded"})
    confirmed = emit(
        action_id="charge-confirm",
        effect=EffectRecord(
            status="confirmed",
            type="stripe_charge",
            request_digest=req_digest,
            response_digest=resp_digest,
        ),
        prior_capsule_id=dispatched["capsule_id"],
        chain_relation="confirms",
        **kw,
    )
    assert verify(confirmed).ok
    assert confirmed["assurance"]["effect_mode"] == "confirmed"
    assert confirmed["assurance"]["ledger_mode"] == "chained"
    assert confirmed["chain"]["parent_capsule_id"] == dispatched["capsule_id"]
    assert confirmed["chain"]["relation"] == "confirms"


def test_model_attestation_dataclass_validation():
    with pytest.raises(InvariantError):
        ModelAttestation(model_id="", provider="anthropic")
    with pytest.raises(InvariantError):
        ModelAttestation(model_id="gpt-4", provider="")
    with pytest.raises((InvariantError, TypeError)):
        ModelAttestation(model_id="gpt-4", provider="openai", compute_attestation="not-a-dict")


# ---------------------------------------------------------------------------
# Adapter-tier API tests (action_type="fyi", no model_id)
# ---------------------------------------------------------------------------


def test_emit_simple_returns_sealed_dict():
    capsule = emit(operator="ACME-CO", developer="agent@v1")
    assert isinstance(capsule, dict)
    assert "capsule_id" in capsule
    assert len(capsule["capsule_id"]) == 64


def test_emit_simple_capsule_id_is_stable():
    capsule = emit(operator="ACME-CO", developer="agent@v1")
    recomputed = compute_capsule_id(capsule)
    assert recomputed == capsule["capsule_id"]


def test_emit_simple_required_fields():
    capsule = emit(operator="ACME-CO", developer="agent@v1", tool_name="my_tool")
    assert capsule["spec_version"] == SPEC_VERSION
    assert capsule["format_version"] == FORMAT_VERSION
    assert capsule["spec_version"] == "draft-mih-scitt-agent-action-capsule-05"
    assert capsule["format_version"] == "4"
    assert capsule["canonicalization_id"] == "jcs"
    assert capsule["operator"] == "ACME-CO"
    assert capsule["developer"] == "agent@v1"
    assert capsule["action_type"] == "fyi"
    assert "timestamp" in capsule


def test_emit_simple_disposition_is_accept():
    capsule = emit(operator="ACME-CO", developer="agent@v1", tool_name="search_web")
    assert capsule["disposition"]["decision"] == "accept"
    assert capsule["disposition"]["approver"] == "policy"
    assert capsule["disposition"]["human_disposed"] is False
    assert capsule["disposition"]["verdict_class"] == "executed"


def test_emit_simple_chaining():
    parent_id = "a" * 64
    capsule = emit(
        operator="ACME-CO",
        developer="agent@v1",
        tool_name="my_tool",
        prior_capsule_id=parent_id,
    )
    assert capsule["chain"]["parent_capsule_id"] == parent_id
    assert capsule["chain"]["relation"] == "sequence"


def test_emit_simple_no_chain_when_none():
    capsule = emit(operator="ACME-CO", developer="agent@v1")
    assert "chain" not in capsule


def test_emit_simple_capsule_verifies():
    capsule = emit(operator="ACME-CO", developer="agent@v1", tool_name="my_tool")
    result = verify(capsule)
    assert result.ok, result.findings


def test_emit_rejects_vintage_format():
    with pytest.raises(InvariantError, match="verification-only"):
        emit(operator="ACME-CO", developer="agent@v1", format_version="2")


def test_emit_simple_custom_action_id_and_timestamp():
    capsule = emit(
        operator="ACME-CO",
        developer="agent@v1",
        action_id="test-action-42",
        timestamp="2026-06-16T00:00:00Z",
    )
    assert capsule["action_id"] == "test-action-42"
    assert capsule["timestamp"] == "2026-06-16T00:00:00Z"


def test_emit_simple_capsule_tamper_changes_id():
    capsule = emit(operator="ACME-CO", developer="agent@v1")
    original_id = capsule["capsule_id"]
    mutated = dict(capsule)
    mutated["operator"] = "EVIL-CO"
    new_id = compute_capsule_id(mutated)
    assert new_id != original_id


def test_emit_tool_name_in_action_id():
    capsule = emit(operator="ACME-CO", developer="agent@v1", tool_name="search_web")
    assert "search_web" in capsule["action_id"]
