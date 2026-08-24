# SPDX-License-Identifier: Apache-2.0
"""LangChain callback listener tests — framework-free core + optional shell.

Sealing logic lives in LangChainListenerCore (duck-typed args, run_id-keyed
pairing), so the full behavior is exercised WITHOUT langchain installed
(mirrors the CrewAI listener test approach). The BaseCallbackHandler shell is
covered by importorskip'd tests at the bottom, including a real
langchain-core tool invocation end-to-end.

Covered:
- tool start → planned capsule (effect.status="planned"), verifies
- tool end → confirmed capsule, confirms-chains the planned id, verifies
- tool error → verdict="errored", effect.status="failed", chained (the
  upgrade over LangChainCapsuleEmitter, which dropped errors)
- run_id-exact pairing for concurrent same-tool calls (no FIFO ambiguity)
- unmatched end emits unchained (no fabricated chain link)
- root-only chain lifecycle capsules on/off via include_lifecycle
- LLM events off by default, on via include_llm; model auto-capture threads
  into the next tool capsule either way
- emission failure warns, never raises
- max_pending bound holds (oldest evicted)
- float args fail closed but do not crash the host app
"""
from __future__ import annotations

import json
import uuid
import warnings

import pytest
from agent_action_capsule import verify

from capsule_emit.adapters.langchain_listener import LangChainListenerCore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> LangChainListenerCore:
    return LangChainListenerCore(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


SER = {"name": "write_po"}
ARGS = {"po": "PO-1", "amount": "120.00"}


# ---------------------------------------------------------------------------
# Tool events → capsules
# ---------------------------------------------------------------------------


def test_tool_start_seals_planned(tmp_path):
    core = _core(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, ARGS, rid)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    cap = caps[0]
    assert cap["effect"]["status"] == "planned"
    assert "write_po" in cap["action_id"]
    assert verify(cap).ok


def test_tool_end_confirms_chain(tmp_path):
    core = _core(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, ARGS, rid)
    planned_id = core.last.capsule_id
    core.on_tool_end_core("ok: PO-1 written", rid)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    confirmed = caps[1]
    assert confirmed["effect"]["status"] == "confirmed"
    assert confirmed["chain"]["parent_capsule_id"] == planned_id
    assert verify(confirmed).ok


def test_tool_error_seals_failed_chained(tmp_path):
    core = _core(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, ARGS, rid)
    planned_id = core.last.capsule_id
    core.on_tool_error_core(RuntimeError("boom"), rid)
    caps = _ledger(tmp_path)
    failed = caps[1]
    assert failed["effect"]["status"] == "failed"
    assert failed["chain"]["parent_capsule_id"] == planned_id
    assert verify(failed).ok


def test_run_id_exact_pairing_concurrent_same_tool(tmp_path):
    """Two concurrent runs of the same tool pair by run_id, not FIFO guessing."""
    core = _core(tmp_path)
    r1, r2 = uuid.uuid4(), uuid.uuid4()
    core.on_tool_start_core(SER, ARGS, r1)
    p1 = core.last.capsule_id
    core.on_tool_start_core(SER, ARGS, r2)
    p2 = core.last.capsule_id
    # resolve in reverse order — run_id keying must still pair correctly
    core.on_tool_end_core("second done", r2)
    c2 = core.last
    core.on_tool_end_core("first done", r1)
    c1 = core.last
    caps = {c["capsule_id"]: c for c in _ledger(tmp_path)}
    assert caps[c2.capsule_id]["chain"]["parent_capsule_id"] == p2
    assert caps[c1.capsule_id]["chain"]["parent_capsule_id"] == p1


def test_unmatched_end_emits_unchained(tmp_path):
    core = _core(tmp_path)
    core.on_tool_end_core("orphan output", uuid.uuid4())
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert "chain" not in caps[0] or not caps[0].get("chain", {}).get("prior_capsule_id")


# ---------------------------------------------------------------------------
# Chain lifecycle (root only)
# ---------------------------------------------------------------------------


def test_root_chain_lifecycle_default_on(tmp_path):
    core = _core(tmp_path)
    rid = uuid.uuid4()
    core.on_chain_lifecycle_core("started", {"q": "hi"}, rid, None)
    core.on_chain_lifecycle_core("completed", "done", rid, None)
    caps = _ledger(tmp_path)
    assert "chain_started" in caps[0]["action_id"] and "chain_completed" in caps[1]["action_id"]


def test_nested_chain_runs_ignored(tmp_path):
    core = _core(tmp_path)
    core.on_chain_lifecycle_core("started", {"q": "hi"}, uuid.uuid4(), uuid.uuid4())
    assert _ledger(tmp_path) == []


def test_lifecycle_off_when_gated(tmp_path):
    core = _core(tmp_path, include_lifecycle=False)
    core.on_chain_lifecycle_core("started", {"q": "hi"}, uuid.uuid4(), None)
    assert _ledger(tmp_path) == []


def test_chain_failed_verdict_errored(tmp_path):
    core = _core(tmp_path)
    core.on_chain_lifecycle_core("failed", RuntimeError("nope"), uuid.uuid4(), None)
    caps = _ledger(tmp_path)
    assert caps[0]["disposition"]["verdict_class"] == "errored"


# ---------------------------------------------------------------------------
# LLM events + model auto-capture
# ---------------------------------------------------------------------------


def test_llm_events_off_by_default_but_model_threads(tmp_path):
    core = _core(tmp_path)
    core.on_llm_start_core({"kwargs": {"model_name": "claude-sonnet-5"}, "name": "ChatAnthropic"})
    assert _ledger(tmp_path) == []  # no llm capsule
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, ARGS, rid)
    cap = _ledger(tmp_path)[0]
    assert cap["model_attestation"]["model_id"] == "claude-sonnet-5"
    assert cap["model_attestation"]["provider"] == "anthropic"


def test_llm_events_on_when_enabled(tmp_path):
    core = _core(tmp_path, include_llm=True)
    core.on_llm_start_core({"kwargs": {"model_name": "gpt-x"}, "name": "ChatOpenAI"})
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert "llm_call_started" in caps[0]["action_id"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_emit_failure_warns_never_raises(tmp_path, monkeypatch):
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_start_core(SER, ARGS, uuid.uuid4())  # must not raise
        core.on_tool_end_core("out", uuid.uuid4())  # must not raise
    assert any("failed to seal" in str(w.message) for w in caught)


def test_max_pending_bound_evicts_oldest(tmp_path):
    core = _core(tmp_path, max_pending=2)
    rids = [uuid.uuid4() for _ in range(4)]
    for rid in rids:
        core.on_tool_start_core(SER, ARGS, rid)
    assert len(core._pending) == 2
    assert rids[0] not in core._pending and rids[1] not in core._pending
    assert rids[2] in core._pending and rids[3] in core._pending


def test_float_args_fail_closed_but_do_not_crash(tmp_path):
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_start_core(SER, {"amount": 120.5}, uuid.uuid4())
    assert core.last is None
    assert any("failed to seal" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Shell (only when langchain-core is installed) — real tool invocation e2e
# ---------------------------------------------------------------------------


def test_shell_real_langchain_tool_e2e(tmp_path):
    lc_tools = pytest.importorskip("langchain_core.tools")
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    listener = LangChainCapsuleListener(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )

    @lc_tools.tool
    def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    out = get_price.invoke({"sku": "SKU-9"}, config={"callbacks": [listener]})
    assert "SKU-9" in out
    caps = _ledger(tmp_path)
    assert len(caps) == 2  # planned + confirmed through the REAL callback manager
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_shell_real_langchain_tool_error_e2e(tmp_path):
    lc_tools = pytest.importorskip("langchain_core.tools")
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    listener = LangChainCapsuleListener(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )

    @lc_tools.tool
    def submit_order(po: str) -> str:
        """Submit a purchase order."""
        raise RuntimeError("order gateway down")

    with pytest.raises(RuntimeError):
        submit_order.invoke({"po": "PO-7"}, config={"callbacks": [listener]})
    caps = _ledger(tmp_path)
    assert len(caps) == 2  # planned + failed — errors become evidence
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
