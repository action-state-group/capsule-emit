# SPDX-License-Identifier: Apache-2.0
"""Tests for §-02 domain / provenance / self_reported_reasoning fields.

Acceptance criteria:
- Round-trip: emit with domain/provenance/self_reported_reasoning_digest →
  verify → ok, fields present in sealed dict.
- Backward-compat: a -01 capsule (no domain/provenance) still verifies ok.
- Unknown value is info, not error.
- Type violation (non-string domain/provenance) is an error.
- self_reported_reasoning.digest malformed → InvariantError at construction.
- capsule_id commits domain/provenance/self_reported_reasoning (tamper = mismatch).
"""
import hashlib

import pytest
from agent_action_capsule import verify
from agent_action_capsule.contracts import (
    DOMAIN_VALUES,
    PROVENANCE_RANK,
    PROVENANCE_VALUES,
    InvariantError,
    SelfReportedReasoning,
)
from agent_action_capsule.parse import parse_capsule

from capsule_emit.builder import build_capsule as emit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = dict(
    action_id="act-domain-01",
    action_type="decide",
    operator="test-org",
    developer="test-agent@1.0",
    model_id="claude-sonnet-4-6",
    provider="anthropic",
)

_COT = b"I decided to call the tool because the user asked for it."
_COT_DIGEST = hashlib.sha256(_COT).hexdigest()


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------


def test_domain_values_frozen():
    assert DOMAIN_VALUES == frozenset({"action", "memory", "reasoning"})


def test_provenance_values_frozen():
    assert PROVENANCE_VALUES == frozenset({"gate", "runtime", "collector"})


def test_provenance_rank_order():
    assert PROVENANCE_RANK["gate"] > PROVENANCE_RANK["runtime"] > PROVENANCE_RANK["collector"]


# ---------------------------------------------------------------------------
# emit() round-trips
# ---------------------------------------------------------------------------


def test_emit_domain_action():
    c = emit(**_BASE, domain="action")
    assert c["domain"] == "action"
    r = verify(c)
    assert r.ok


def test_emit_domain_memory():
    c = emit(**_BASE, domain="memory")
    assert c["domain"] == "memory"
    assert verify(c).ok


def test_emit_domain_reasoning():
    base_fyi = {k: v for k, v in _BASE.items() if k != "action_type"}
    c = emit(**base_fyi, domain="reasoning", action_type="fyi")
    assert c["domain"] == "reasoning"
    assert verify(c).ok


def test_emit_provenance_gate():
    c = emit(**_BASE, provenance="gate")
    assert c["provenance"] == "gate"
    assert verify(c).ok


def test_emit_provenance_runtime():
    c = emit(**_BASE, provenance="runtime")
    assert c["provenance"] == "runtime"
    assert verify(c).ok


def test_emit_provenance_collector():
    c = emit(**_BASE, provenance="collector")
    assert c["provenance"] == "collector"
    assert verify(c).ok


def test_emit_domain_and_provenance():
    c = emit(**_BASE, domain="action", provenance="gate")
    assert c["domain"] == "action"
    assert c["provenance"] == "gate"
    assert verify(c).ok


def test_emit_self_reported_reasoning():
    c = emit(**_BASE, self_reported_reasoning_digest=_COT_DIGEST)
    assert c["self_reported_reasoning"]["digest"] == _COT_DIGEST
    assert verify(c).ok


def test_emit_all_new_fields():
    c = emit(**_BASE, domain="action", provenance="runtime", self_reported_reasoning_digest=_COT_DIGEST)
    assert c["domain"] == "action"
    assert c["provenance"] == "runtime"
    assert c["self_reported_reasoning"]["digest"] == _COT_DIGEST
    assert verify(c).ok


# ---------------------------------------------------------------------------
# capsule_id tamper-evidence
# ---------------------------------------------------------------------------


def test_domain_committed_to_capsule_id():
    c = emit(**_BASE, domain="action")
    tampered = dict(c)
    tampered["domain"] = "memory"
    r = verify(tampered)
    assert not r.ok
    codes = [f.code for f in r.findings]
    assert "capsule_id_mismatch" in codes


def test_provenance_committed_to_capsule_id():
    c = emit(**_BASE, provenance="gate")
    tampered = dict(c)
    tampered["provenance"] = "collector"
    r = verify(tampered)
    assert not r.ok
    assert any(f.code == "capsule_id_mismatch" for f in r.findings)


def test_self_reported_reasoning_committed_to_capsule_id():
    c = emit(**_BASE, self_reported_reasoning_digest=_COT_DIGEST)
    tampered = dict(c)
    tampered["self_reported_reasoning"] = {"digest": "a" * 64}
    r = verify(tampered)
    assert not r.ok
    assert any(f.code == "capsule_id_mismatch" for f in r.findings)


# ---------------------------------------------------------------------------
# Backward-compat: -01 capsules still verify ok
# ---------------------------------------------------------------------------


def test_minus_01_capsule_verifies():
    c = emit(
        **_BASE,
        spec_version="draft-mih-scitt-agent-action-capsule-01",
    )
    assert "domain" not in c
    assert "provenance" not in c
    r = verify(c)
    assert r.ok


# ---------------------------------------------------------------------------
# Unknown value → info, not error
# ---------------------------------------------------------------------------


def test_unknown_domain_value_is_info():
    c = emit(**_BASE, domain="workflow")
    r = verify(c)
    assert r.ok
    info_codes = [f.code for f in r.findings if f.severity == "info"]
    assert "domain_unknown_value" in info_codes


def test_unknown_provenance_value_is_info():
    c = emit(**_BASE, provenance="hardware_tee")
    r = verify(c)
    assert r.ok
    info_codes = [f.code for f in r.findings if f.severity == "info"]
    assert "provenance_unknown_value" in info_codes


def test_x_prefixed_domain_no_info():
    c = emit(**_BASE, domain="x-custom-domain")
    r = verify(c)
    assert r.ok
    assert not any(f.code == "domain_unknown_value" for f in r.findings)


def test_x_prefixed_provenance_no_info():
    c = emit(**_BASE, provenance="x-hardware-tee")
    r = verify(c)
    assert r.ok
    assert not any(f.code == "provenance_unknown_value" for f in r.findings)


# ---------------------------------------------------------------------------
# Type violations → error
# ---------------------------------------------------------------------------


def test_non_string_domain_is_error():
    c = emit(**_BASE)
    tampered = dict(c)
    tampered["domain"] = 42
    r = verify(tampered)
    assert not r.ok
    assert any(f.code == "domain_not_string" for f in r.findings)


def test_non_string_provenance_is_error():
    c = emit(**_BASE)
    tampered = dict(c)
    tampered["provenance"] = ["gate"]
    r = verify(tampered)
    assert not r.ok
    assert any(f.code == "provenance_not_string" for f in r.findings)


# ---------------------------------------------------------------------------
# SelfReportedReasoning dataclass invariants
# ---------------------------------------------------------------------------


def test_self_reported_reasoning_bad_digest():
    with pytest.raises(InvariantError, match="64-hex"):
        SelfReportedReasoning(digest="not-hex")


def test_self_reported_reasoning_ok():
    srr = SelfReportedReasoning(digest=_COT_DIGEST)
    assert srr.digest == _COT_DIGEST


def test_self_reported_reasoning_bad_emit_digest():
    with pytest.raises(InvariantError):
        emit(**_BASE, self_reported_reasoning_digest="short")


# ---------------------------------------------------------------------------
# parse_capsule round-trip
# ---------------------------------------------------------------------------


def test_parse_capsule_domain_provenance():
    c = emit(**_BASE, domain="memory", provenance="collector")
    parsed = parse_capsule(c)
    assert parsed.domain == "memory"
    assert parsed.provenance == "collector"


def test_parse_capsule_self_reported_reasoning():
    c = emit(**_BASE, self_reported_reasoning_digest=_COT_DIGEST)
    parsed = parse_capsule(c)
    assert parsed.self_reported_reasoning is not None
    assert parsed.self_reported_reasoning.digest == _COT_DIGEST


def test_parse_capsule_none_when_absent():
    c = emit(**_BASE)
    parsed = parse_capsule(c)
    assert parsed.domain is None
    assert parsed.provenance is None
    assert parsed.self_reported_reasoning is None
