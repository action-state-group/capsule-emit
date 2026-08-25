# SPDX-License-Identifier: Apache-2.0
"""CrewAI event-bus listener tests — framework-free core + optional shell.

The sealing logic lives in CrewAIListenerCore, which takes duck-typed event
objects, so the full behavior is exercised here WITHOUT crewai installed
(mirrors the dapr_agents synthetic-stub approach). The thin BaseEventListener
shell is covered by an importorskip'd registration test at the bottom.

Covered:
- tool started → planned capsule (effect.status="planned"), verifies
- tool finished → confirmed capsule, confirms-chains the planned id, verifies
- tool error → verdict="errored", effect.status="failed", chained, verifies
- input digest identical across the planned/confirmed pair (same input)
- FIFO pairing for concurrent same-tool same-args calls
- unmatched finish emits unchained (no fabricated chain link)
- replay guard: replay_check=True → zero capsules
- broken replay probe → evidence still seals (guard fails open to sealing)
- lifecycle fyi capsules on/off via include_lifecycle
- LLM events off by default, on via include_llm
- emission failure warns, never raises (belt over their bus's isolation)
- max_pending bound holds
"""
from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from capsule_emit.adapters.crewai import CrewAIListenerCore
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> CrewAIListenerCore:
    return CrewAIListenerCore(
        operator="acme-co",
        developer="ops-crew@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _started(tool="write_po", args=None):
    return SimpleNamespace(tool_name=tool, tool_args=args or {"po": "PO-1", "amount": "120.00"})


def _finished(tool="write_po", args=None, output="ok: PO-1 written"):
    return SimpleNamespace(
        tool_name=tool, tool_args=args or {"po": "PO-1", "amount": "120.00"}, output=output
    )


def _error(tool="write_po", args=None, error="boom"):
    return SimpleNamespace(
        tool_name=tool, tool_args=args or {"po": "PO-1", "amount": "120.00"}, error=error
    )


# ---------------------------------------------------------------------------
# Tool events → capsules
# ---------------------------------------------------------------------------


def test_tool_started_seals_planned(tmp_path):
    core = _core(tmp_path)
    core.on_tool_started(_started())
    cap = core.last.capsule
    assert cap["effect"]["status"] == "planned"
    assert cap["effect"]["type"] == "write_po"
    assert cap["action_type"] == "fyi"
    assert verify(cap).ok


def test_tool_finished_confirms_chain(tmp_path):
    core = _core(tmp_path)
    core.on_tool_started(_started())
    planned_id = core.last.capsule_id
    core.on_tool_finished(_finished())
    cap = core.last.capsule
    assert cap["effect"]["status"] == "confirmed"
    assert cap["chain"]["parent_capsule_id"] == planned_id
    assert cap["chain"]["relation"] == "confirms"
    assert verify(cap).ok


def test_tool_error_seals_failed(tmp_path):
    core = _core(tmp_path)
    core.on_tool_started(_started())
    planned_id = core.last.capsule_id
    core.on_tool_error(_error())
    cap = core.last.capsule
    assert cap["effect"]["status"] == "failed"
    assert cap["disposition"]["verdict_class"] == "errored"
    assert cap["chain"]["parent_capsule_id"] == planned_id
    assert verify(cap).ok


def test_input_digest_stable_across_pair(tmp_path):
    core = _core(tmp_path)
    core.on_tool_started(_started())
    planned = core.last.capsule
    core.on_tool_finished(_finished())
    confirmed = core.last.capsule
    p_digest = planned["model_attestation"]["compute_attestation"]["agent_input_digest"]
    c_digest = confirmed["model_attestation"]["compute_attestation"]["agent_input_digest"]
    assert p_digest == c_digest


def test_fifo_pairing_concurrent_same_tool(tmp_path):
    core = _core(tmp_path)
    core.on_tool_started(_started())
    first_planned = core.last.capsule_id
    core.on_tool_started(_started())
    second_planned = core.last.capsule_id
    assert first_planned != second_planned  # distinct capsules (uuid action_id)
    core.on_tool_finished(_finished())
    assert core.last.capsule["chain"]["parent_capsule_id"] == first_planned
    core.on_tool_finished(_finished())
    assert core.last.capsule["chain"]["parent_capsule_id"] == second_planned


def test_unmatched_finish_emits_unchained(tmp_path):
    core = _core(tmp_path)
    core.on_tool_finished(_finished())
    cap = core.last.capsule
    assert cap["effect"]["status"] == "confirmed"
    assert "chain" not in cap or not cap.get("chain")
    assert verify(cap).ok


# ---------------------------------------------------------------------------
# Replay guard
# ---------------------------------------------------------------------------


def test_replay_guard_suppresses_all(tmp_path):
    core = _core(tmp_path, replay_check=lambda: True)
    core.on_tool_started(_started())
    core.on_tool_finished(_finished())
    core.on_tool_error(_error())
    core.on_crew_kickoff(SimpleNamespace(crew_name="c"), "started")
    assert core.last is None
    assert core.results == []


def test_broken_replay_probe_still_seals(tmp_path):
    def _broken():
        raise RuntimeError("probe down")

    core = _core(tmp_path, replay_check=_broken)
    core.on_tool_started(_started())
    assert core.last is not None  # a broken probe must not silence evidence


# ---------------------------------------------------------------------------
# Lifecycle + LLM gating
# ---------------------------------------------------------------------------


def test_lifecycle_fyi_capsules_default_on(tmp_path):
    core = _core(tmp_path)
    core.on_crew_kickoff(SimpleNamespace(crew_name="research-crew"), "started")
    cap = core.last.capsule
    assert cap["action_type"] == "fyi"
    assert "crew_kickoff_started" in cap["action_id"]
    assert verify(cap).ok


def test_lifecycle_off_when_gated(tmp_path):
    core = _core(tmp_path, include_lifecycle=False)
    core.on_crew_kickoff(SimpleNamespace(crew_name="c"), "started")
    assert core.last is None


def test_crew_failed_verdict_errored(tmp_path):
    core = _core(tmp_path)
    core.on_crew_kickoff(SimpleNamespace(crew_name="c", error="LLM down"), "failed")
    assert core.last.capsule["disposition"]["verdict_class"] == "errored"


def test_llm_events_off_by_default(tmp_path):
    core = _core(tmp_path)
    core.on_llm_call(SimpleNamespace(model="gpt-x"), "started")
    assert core.last is None


def test_llm_events_on_when_enabled(tmp_path):
    core = _core(tmp_path, include_llm=True)
    core.on_llm_call(SimpleNamespace(model="gpt-x"), "started")
    assert "llm_call_started" in core.last.capsule["action_id"]


# ---------------------------------------------------------------------------
# Never-raise + bounds
# ---------------------------------------------------------------------------


def test_emit_failure_warns_never_raises(tmp_path, monkeypatch):
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_started(_started())   # must not raise
        core.on_tool_finished(_finished())  # must not raise
    assert any("failed to seal" in str(w.message) for w in caught)


def test_max_pending_bound(tmp_path):
    core = _core(tmp_path, max_pending=2)
    for _ in range(5):
        core.on_tool_started(_started())
    key = ("write_po", core._args_key({"po": "PO-1", "amount": "120.00"}))
    assert len(core._pending[key]) == 2  # bounded, oldest evicted


def test_float_args_fail_closed_but_do_not_crash(tmp_path):
    """Raw float in tool_args → JCS digest fails closed inside emit();
    the listener warns and the crew is unaffected (no capsule sealed)."""
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_started(_started(args={"amount": 120.5}))
    assert core.last is None
    assert any("failed to seal" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Shell registration (only when crewai is installed)
# ---------------------------------------------------------------------------


def test_shell_registers_on_real_bus(tmp_path):
    pytest.importorskip("crewai", reason="crewai not installed")
    from capsule_emit.adapters.crewai_listener import CapsuleEventListener

    listener = CapsuleEventListener(
        operator="acme-co",
        developer="ops-crew@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )
    try:
        from crewai.events import ToolUsageStartedEvent, crewai_event_bus
    except ImportError:
        from crewai.utilities.events import ToolUsageStartedEvent, crewai_event_bus

    future = crewai_event_bus.emit(
        None,
        ToolUsageStartedEvent(tool_name="write_po", tool_args={"po": "PO-1"}),
    )
    if future is not None:  # async bus (crewai >= 1.x) returns a Future
        future.result(timeout=10)
    assert listener.core.last is not None
    assert listener.core.last.capsule["effect"]["status"] == "planned"
    # replay guard wired to the real module-level probe
    assert listener.core._replay_check is not None
