# SPDX-License-Identifier: Apache-2.0
"""Tests for the capsule-emit gate (wicket) module.

Covers:
- run_gate: all pass -> GateResult.passed True
- run_gate: one fail -> GateResult.passed False, reason captured in CheckResult
- gate_and_emit pass case: emitter.emit_capsule called with verdict="executed", gate_checks in capsule
- gate_and_emit block + no callback: raises GateBlockedError
- gate_and_emit block + callback: callback called, emit_capsule called with verdict="blocked"
- Constraint protocol: duck-typed class satisfies isinstance(obj, Constraint)
- AmountUnderCap: under cap -> (True, None); over cap -> (False, reason_str)
- VendorKnown: known -> (True, None); unknown -> (False, reason_str)
- MCP adapter: @emitter.tool(constraints=[AmountUnderCap(5000)]) passing input -> gate_checks in capsule
- MCP adapter: @emitter.tool(constraints=[AmountUnderCap(100)]) failing input -> GateBlockedError raised
"""
from __future__ import annotations

import pytest
from agent_action_capsule import verify

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.constraints.apache import AmountUnderCap, VendorKnown
from capsule_emit.gate import (
    CheckResult,
    Constraint,
    GateBlockedError,
    GateResult,
    gate_and_emit,
    run_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emitter(tmp_path, **kw) -> MCPCapsuleEmitter:
    return MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ca(emitter: MCPCapsuleEmitter) -> dict:
    return emitter.last.capsule["model_attestation"]["compute_attestation"]


# ---------------------------------------------------------------------------
# Simple constraint fixtures
# ---------------------------------------------------------------------------


class _AlwaysPass:
    name = "always_pass"

    def check(self, inputs, output):
        return (True, None)


class _AlwaysFail:
    name = "always_fail"

    def check(self, inputs, output):
        return (False, "deliberate failure")


class _Raises:
    name = "raises_on_check"

    def check(self, inputs, output):
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# run_gate: basic pass / fail
# ---------------------------------------------------------------------------


def test_run_gate_all_pass():
    """All-pass constraints -> GateResult.passed is True."""
    result = run_gate([_AlwaysPass(), _AlwaysPass()], inputs={}, output=None)
    assert result.passed is True
    assert all(r.passed for r in result.results)
    assert len(result.results) == 2


def test_run_gate_one_fail():
    """One failing constraint -> GateResult.passed is False."""
    result = run_gate([_AlwaysPass(), _AlwaysFail()], inputs={}, output=None)
    assert result.passed is False


def test_run_gate_fail_reason_captured():
    """Failing constraint's reason is captured in CheckResult."""
    result = run_gate([_AlwaysFail()], inputs={}, output=None)
    assert len(result.results) == 1
    cr = result.results[0]
    assert cr.name == "always_fail"
    assert cr.passed is False
    assert "deliberate failure" in (cr.reason or "")


def test_run_gate_empty_constraints():
    """Zero constraints -> trivially passes."""
    result = run_gate([], inputs={"amount": 1e9}, output=None)
    assert result.passed is True
    assert result.results == []


def test_run_gate_constraint_raises_treated_as_fail():
    """A constraint that raises is treated as a failure, not a crash."""
    result = run_gate([_Raises()], inputs={}, output=None)
    assert result.passed is False
    assert result.results[0].reason is not None
    assert "boom" in result.results[0].reason


def test_run_gate_all_evaluated_no_short_circuit():
    """All constraints run even when an earlier one fails."""
    result = run_gate(
        [_AlwaysFail(), _AlwaysPass(), _AlwaysFail()],
        inputs={},
        output=None,
    )
    assert len(result.results) == 3
    names = [r.name for r in result.results]
    assert "always_pass" in names


# ---------------------------------------------------------------------------
# GateResult helpers
# ---------------------------------------------------------------------------


def test_gate_result_to_gate_checks_serialises():
    """to_gate_checks() returns a list of dicts with name/passed(/reason)."""
    result = run_gate([_AlwaysPass(), _AlwaysFail()], inputs={}, output=None)
    checks = result.to_gate_checks()
    assert len(checks) == 2
    assert all(isinstance(c, dict) for c in checks)
    assert all("name" in c and "passed" in c for c in checks)
    # Passing check should not have 'reason' key (or reason is None)
    passing = next(c for c in checks if c["passed"])
    assert "reason" not in passing or passing.get("reason") is None
    # Failing check should have reason
    failing = next(c for c in checks if not c["passed"])
    assert "reason" in failing
    assert failing["reason"] is not None


# ---------------------------------------------------------------------------
# gate_and_emit: pass case
# ---------------------------------------------------------------------------


def test_gate_and_emit_pass_calls_emit_with_executed(tmp_path):
    """All constraints pass -> emit_capsule called with verdict='executed'."""
    emitter = _emitter(tmp_path)
    output = gate_and_emit(
        action="write_po",
        constraints=[_AlwaysPass()],
        inputs={"vendor": "Acme", "amount": 100},
        output={"po_id": "PO-001"},
        emitter=emitter,
    )
    assert output == {"po_id": "PO-001"}  # pass-through
    assert emitter.last is not None
    assert emitter.last.capsule["disposition"]["verdict_class"] == "executed"


def test_gate_and_emit_pass_gate_checks_in_compute_attestation(tmp_path):
    """Passed gate puts gate_checks into compute_attestation."""
    emitter = _emitter(tmp_path)
    gate_and_emit(
        action="write_po",
        constraints=[_AlwaysPass()],
        inputs={"vendor": "Acme"},
        output=None,
        emitter=emitter,
    )
    ca = _ca(emitter)
    assert "gate_checks" in ca
    checks = ca["gate_checks"]
    assert isinstance(checks, list)
    assert len(checks) == 1
    assert checks[0]["name"] == "always_pass"
    assert checks[0]["passed"] is True


def test_gate_and_emit_pass_capsule_verifies(tmp_path):
    """Passed gate seals a verifiable capsule."""
    emitter = _emitter(tmp_path)
    gate_and_emit(
        action="write_po",
        constraints=[_AlwaysPass()],
        inputs={"vendor": "Acme", "amount": 100},
        output={"status": "ok"},
        emitter=emitter,
    )
    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# gate_and_emit: blocked, no callback -> GateBlockedError
# ---------------------------------------------------------------------------


def test_gate_and_emit_block_no_callback_raises(tmp_path):
    """Blocked gate + no on_block -> raises GateBlockedError."""
    emitter = _emitter(tmp_path)
    with pytest.raises(GateBlockedError) as exc_info:
        gate_and_emit(
            action="write_po",
            constraints=[_AlwaysFail()],
            inputs={"vendor": "EvilCorp", "amount": 9999},
            output=None,
            emitter=emitter,
            on_block=None,
        )
    err = exc_info.value
    assert err.action == "write_po"
    assert err.gate_result.passed is False


def test_gate_blocked_error_no_capsule_emitted(tmp_path):
    """When GateBlockedError is raised, no capsule is emitted."""
    emitter = _emitter(tmp_path)
    with pytest.raises(GateBlockedError):
        gate_and_emit(
            action="write_po",
            constraints=[_AlwaysFail()],
            inputs={},
            output=None,
            emitter=emitter,
        )
    assert emitter.last is None
    assert read_ledger(tmp_path / "ledger.jsonl") == []


# ---------------------------------------------------------------------------
# gate_and_emit: blocked + callback
# ---------------------------------------------------------------------------


def test_gate_and_emit_block_with_callback_fires_callback(tmp_path):
    """Blocked gate + on_block -> callback is called."""
    emitter = _emitter(tmp_path)
    callback_calls: list[tuple] = []

    def _on_block(action, gate_result):
        callback_calls.append((action, gate_result))

    gate_and_emit(
        action="write_po",
        constraints=[_AlwaysFail()],
        inputs={},
        output=None,
        emitter=emitter,
        on_block=_on_block,
    )

    assert len(callback_calls) == 1
    assert callback_calls[0][0] == "write_po"
    assert isinstance(callback_calls[0][1], GateResult)
    assert callback_calls[0][1].passed is False


def test_gate_and_emit_block_with_callback_emits_blocked_capsule(tmp_path):
    """Blocked gate + on_block -> emit_capsule called with verdict='blocked'."""
    emitter = _emitter(tmp_path)

    gate_and_emit(
        action="write_po",
        constraints=[_AlwaysFail()],
        inputs={},
        output=None,
        emitter=emitter,
        on_block=lambda a, gr: None,
    )

    assert emitter.last is not None
    capsule = emitter.last.capsule
    assert capsule["disposition"]["verdict_class"] == "blocked"
    assert capsule.get("effect", {}).get("status") == "planned"


def test_gate_and_emit_block_with_callback_gate_checks_in_capsule(tmp_path):
    """Blocked capsule carries gate_checks in compute_attestation."""
    emitter = _emitter(tmp_path)

    gate_and_emit(
        action="write_po",
        constraints=[_AlwaysFail()],
        inputs={"vendor": "EvilCorp"},
        output=None,
        emitter=emitter,
        on_block=lambda a, gr: None,
    )

    ca = _ca(emitter)
    assert "gate_checks" in ca
    checks = ca["gate_checks"]
    assert len(checks) == 1
    assert checks[0]["passed"] is False


def test_gate_and_emit_block_returns_output(tmp_path):
    """Blocked gate with callback returns output unchanged."""
    emitter = _emitter(tmp_path)
    result = gate_and_emit(
        action="write_po",
        constraints=[_AlwaysFail()],
        inputs={},
        output={"status": "pending"},
        emitter=emitter,
        on_block=lambda a, gr: None,
    )
    assert result == {"status": "pending"}


# ---------------------------------------------------------------------------
# Constraint protocol: duck-typing
# ---------------------------------------------------------------------------


def test_constraint_protocol_duck_type_passes():
    """A duck-typed class with name + check() satisfies isinstance(obj, Constraint)."""
    assert isinstance(_AlwaysPass(), Constraint)
    assert isinstance(_AlwaysFail(), Constraint)


def test_constraint_protocol_rejects_missing_check():
    """An object missing check() does NOT satisfy Constraint."""
    class _NoCheck:
        name = "no_check"

    assert not isinstance(_NoCheck(), Constraint)


def test_constraint_protocol_rejects_missing_name():
    """An object missing name does NOT satisfy Constraint."""
    class _NoName:
        def check(self, inputs, output):
            return (True, None)

    assert not isinstance(_NoName(), Constraint)


# ---------------------------------------------------------------------------
# AmountUnderCap
# ---------------------------------------------------------------------------


def test_amount_under_cap_passes():
    """AmountUnderCap: amount < cap -> (True, None)."""
    c = AmountUnderCap(5000)
    ok, reason = c.check({"amount": 1200}, None)
    assert ok is True
    assert reason is None


def test_amount_under_cap_fails():
    """AmountUnderCap: amount >= cap -> (False, reason_str)."""
    c = AmountUnderCap(5000)
    ok, reason = c.check({"amount": 5000}, None)
    assert ok is False
    assert reason is not None
    assert "5000" in reason


def test_amount_under_cap_strictly_over():
    """AmountUnderCap: amount > cap -> fails."""
    c = AmountUnderCap(100)
    ok, reason = c.check({"amount": 999}, None)
    assert ok is False
    assert "999" in (reason or "")


def test_amount_under_cap_name():
    """AmountUnderCap: name reflects cap."""
    c = AmountUnderCap(5000)
    assert "5000" in c.name


def test_amount_under_cap_missing_amount():
    """AmountUnderCap: missing amount key defaults to 0 -> passes for positive cap."""
    c = AmountUnderCap(100)
    ok, reason = c.check({}, None)
    assert ok is True


def test_amount_under_cap_satisfies_protocol():
    """AmountUnderCap satisfies Constraint protocol."""
    c = AmountUnderCap(100)
    assert isinstance(c, Constraint)


# ---------------------------------------------------------------------------
# VendorKnown
# ---------------------------------------------------------------------------


def test_vendor_known_passes():
    """VendorKnown: vendor in known set -> (True, None)."""
    c = VendorKnown({"Acme", "Globex"})
    ok, reason = c.check({"vendor": "Acme"}, None)
    assert ok is True
    assert reason is None


def test_vendor_known_fails():
    """VendorKnown: vendor not in known set -> (False, reason_str)."""
    c = VendorKnown({"Acme", "Globex"})
    ok, reason = c.check({"vendor": "EvilCorp"}, None)
    assert ok is False
    assert reason is not None
    assert "EvilCorp" in reason


def test_vendor_known_missing_vendor():
    """VendorKnown: missing vendor key -> fails."""
    c = VendorKnown({"Acme"})
    ok, reason = c.check({}, None)
    assert ok is False


def test_vendor_known_name():
    """VendorKnown: has stable name."""
    c = VendorKnown({"Acme"})
    assert c.name == "vendor_known"


def test_vendor_known_satisfies_protocol():
    """VendorKnown satisfies Constraint protocol."""
    c = VendorKnown({"Acme"})
    assert isinstance(c, Constraint)


# ---------------------------------------------------------------------------
# MCP adapter: @emitter.tool(constraints=[...])
# ---------------------------------------------------------------------------


def test_mcp_tool_with_passing_constraints_gate_checks_in_capsule(tmp_path):
    """@emitter.tool(constraints=[AmountUnderCap(5000)]) with amount=100 -> gate_checks in compute_attestation."""
    emitter = _emitter(tmp_path)

    @emitter.tool(constraints=[AmountUnderCap(5000)])
    def submit_order(vendor: str, amount: float) -> dict:
        return {"status": "ok"}

    submit_order(vendor="Acme", amount=100.0)

    assert emitter.last is not None
    ca = _ca(emitter)
    assert "gate_checks" in ca
    checks = ca["gate_checks"]
    assert len(checks) == 1
    assert checks[0]["passed"] is True
    assert emitter.last.capsule["disposition"]["verdict_class"] == "executed"
    assert verify(emitter.last.capsule).ok


def test_mcp_tool_with_failing_constraints_raises_gate_blocked_error(tmp_path):
    """@emitter.tool(constraints=[AmountUnderCap(100)]) with amount=999 -> GateBlockedError."""
    emitter = _emitter(tmp_path)

    @emitter.tool(constraints=[AmountUnderCap(100)])
    def submit_order(vendor: str, amount: float) -> dict:
        return {"status": "ok"}

    with pytest.raises(GateBlockedError) as exc_info:
        submit_order(vendor="Acme", amount=999.0)

    err = exc_info.value
    assert err.action == "submit_order"
    assert err.gate_result.passed is False
    # No capsule emitted on block-without-callback
    assert emitter.last is None


def test_mcp_tool_no_constraints_existing_path_unchanged(tmp_path):
    """@emitter.tool() with no constraints: existing emit path runs unchanged."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def my_action(x: int) -> int:
        return x * 2

    result = my_action(x=5)
    assert result == 10
    assert emitter.last is not None
    # No gate_checks in capsule
    ca = _ca(emitter)
    assert "gate_checks" not in ca
    assert emitter.last.capsule["disposition"]["verdict_class"] == "executed"
    assert verify(emitter.last.capsule).ok


def test_mcp_tool_with_on_block_callback_blocked_capsule(tmp_path):
    """@emitter.tool(constraints=..., on_block=...) with failing constraint -> on_block fired + blocked capsule."""
    emitter = _emitter(tmp_path)
    calls: list = []

    @emitter.tool(
        constraints=[AmountUnderCap(100)],
        on_block=lambda action, gr: calls.append((action, gr)),
    )
    def submit_order(vendor: str, amount: float) -> dict:
        return {"status": "ok"}

    result = submit_order(vendor="EvilCorp", amount=9999.0)
    assert result == {"status": "ok"}  # pass-through

    # Callback fired
    assert len(calls) == 1
    assert calls[0][0] == "submit_order"

    # Blocked capsule emitted
    assert emitter.last is not None
    capsule = emitter.last.capsule
    assert capsule["disposition"]["verdict_class"] == "blocked"
    ca = _ca(emitter)
    assert "gate_checks" in ca


def test_mcp_tool_multiple_constraints_all_pass(tmp_path):
    """Multiple constraints all pass -> gate_checks has all entries."""
    emitter = _emitter(tmp_path)

    @emitter.tool(
        constraints=[AmountUnderCap(5000), VendorKnown({"Acme", "Globex"})]
    )
    def place_order(vendor: str, amount: float) -> dict:
        return {"ok": True}

    place_order(vendor="Acme", amount=1200.0)

    ca = _ca(emitter)
    checks = ca["gate_checks"]
    assert len(checks) == 2
    assert all(c["passed"] for c in checks)


def test_mcp_tool_multiple_constraints_one_fails(tmp_path):
    """Multiple constraints, one fails -> both appear in gate_checks."""
    emitter = _emitter(tmp_path)

    @emitter.tool(
        constraints=[AmountUnderCap(5000), VendorKnown({"Acme"})],
        on_block=lambda a, gr: None,
    )
    def place_order(vendor: str, amount: float) -> dict:
        return {"ok": True}

    place_order(vendor="EvilCorp", amount=100.0)

    ca = _ca(emitter)
    checks = ca["gate_checks"]
    assert len(checks) == 2
    passing = [c for c in checks if c["passed"]]
    failing = [c for c in checks if not c["passed"]]
    assert len(passing) == 1
    assert len(failing) == 1


def test_mcp_tool_gate_checks_in_ledger(tmp_path):
    """gate_checks persisted in the JSONL ledger row."""
    emitter = _emitter(tmp_path)

    @emitter.tool(constraints=[AmountUnderCap(5000)])
    def submit(vendor: str, amount: float) -> dict:
        return {}

    submit(vendor="Acme", amount=100.0)

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 1
    ca = records[0]["model_attestation"]["compute_attestation"]
    assert "gate_checks" in ca
