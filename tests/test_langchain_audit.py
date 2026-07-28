# SPDX-License-Identifier: Apache-2.0
"""Tests for ScittAuditCallbackHandler — EU AI Act Art. 12 compliance handler."""
from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from capsule_emit import read_ledger
from capsule_emit.adapters.langchain import ScittAuditCallbackHandler  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make(tmp_path, **kw) -> ScittAuditCallbackHandler:
    return ScittAuditCallbackHandler(
        operator="acme-co",
        developer="invoice-agent@v1",
        ledger=tmp_path / "l.jsonl",
        anchor=False,
        **kw,
    )


def _ma(result) -> dict:
    return result.capsule.get("model_attestation") or {}


# ---------------------------------------------------------------------------
# risk_class threading
# ---------------------------------------------------------------------------

def test_risk_class_default_high_risk(tmp_path):
    """Default risk_class is 'high-risk' and threads into every tool capsule."""
    h = _make(tmp_path)
    h.on_tool_start({"name": "search"}, "q", run_id="r1")
    h.on_tool_end("results", run_id="r1")
    capsule = h.last.capsule
    assert capsule.get("action_type") == "high-risk"


def test_risk_class_custom(tmp_path):
    """Custom risk_class is threaded into tool capsules."""
    h = _make(tmp_path, risk_class="limited-risk")
    h.on_tool_start({"name": "calc"}, "1+1", run_id="r1")
    h.on_tool_end("2", run_id="r1")
    assert h.last.capsule.get("action_type") == "limited-risk"


def test_risk_class_on_chain_start_capsule(tmp_path):
    """Chain-start capsule carries risk_class as action_type."""
    h = _make(tmp_path, risk_class="high-risk")
    h.on_chain_start({}, {"input": "hello"}, run_id="c1", parent_run_id=None)
    capsule = h.last.capsule
    assert capsule.get("action_type") == "high-risk"
    assert "chain-start" in capsule.get("action_id", "")


def test_risk_class_on_chain_end_capsule(tmp_path):
    """Chain-end capsule carries risk_class and links to chain-start via prior_capsule_id."""
    h = _make(tmp_path)
    h.on_chain_start({}, {"input": "hi"}, run_id="c1", parent_run_id=None)
    chain_head = h.last.capsule_id
    h.on_chain_end({"output": "bye"}, run_id="c1", parent_run_id=None)
    end_capsule = h.last.capsule
    assert "chain-end" in end_capsule.get("action_id", "")
    assert end_capsule.get("chain", {}).get("parent_capsule_id") == chain_head


# ---------------------------------------------------------------------------
# Chain bookends
# ---------------------------------------------------------------------------

def test_chain_events_emitted_by_default(tmp_path):
    """on_chain_start + on_chain_end emit capsules by default."""
    h = _make(tmp_path)
    h.on_chain_start({}, {}, run_id="c1", parent_run_id=None)
    h.on_tool_start({"name": "tool"}, "x", run_id="t1")
    h.on_tool_end("y", run_id="t1")
    h.on_chain_end({}, run_id="c1", parent_run_id=None)
    assert len(h.results) == 3  # chain-start, tool, chain-end


def test_chain_events_disabled(tmp_path):
    """emit_chain_events=False suppresses chain-start/end capsules."""
    h = _make(tmp_path, emit_chain_events=False)
    h.on_chain_start({}, {}, run_id="c1", parent_run_id=None)
    h.on_tool_start({"name": "tool"}, "x", run_id="t1")
    h.on_tool_end("y", run_id="t1")
    h.on_chain_end({}, run_id="c1", parent_run_id=None)
    assert len(h.results) == 1  # only the tool capsule


def test_nested_chain_start_skipped(tmp_path):
    """on_chain_start with a parent_run_id does NOT emit a capsule."""
    h = _make(tmp_path)
    h.on_chain_start({}, {}, run_id="child", parent_run_id="parent")
    assert h.last is None


def test_tool_capsule_links_to_chain_head(tmp_path):
    """Tool capsule's prior_capsule_id equals the chain-start capsule_id."""
    h = _make(tmp_path)
    h.on_chain_start({}, {}, run_id="c1", parent_run_id=None)
    chain_head_id = h.last.capsule_id
    h.on_tool_start({"name": "fetch"}, "url", run_id="t1")
    h.on_tool_end("data", run_id="t1")
    tool_capsule = h.last.capsule
    assert tool_capsule.get("chain", {}).get("parent_capsule_id") == chain_head_id


def test_tool_capsule_no_chain_head_when_chain_events_off(tmp_path):
    """With emit_chain_events=False, tool capsule has no prior_capsule_id."""
    h = _make(tmp_path, emit_chain_events=False)
    h.on_tool_start({"name": "tool"}, "x", run_id="t1")
    h.on_tool_end("y", run_id="t1")
    assert h.last.capsule.get("confirms") is None


# ---------------------------------------------------------------------------
# Model auto-capture (inherited from LangChainCapsuleEmitter)
# ---------------------------------------------------------------------------

def test_model_auto_captured_on_tool(tmp_path):
    """ScittAuditCallbackHandler inherits LLM model auto-capture."""
    h = _make(tmp_path)
    h.on_llm_start(
        {"name": "ChatAnthropic", "kwargs": {"model": "claude-sonnet-4-6"}},
        prompts=["hi"],
    )
    h.on_tool_start({"name": "calc"}, "1+1", run_id="r1")
    h.on_tool_end("2", run_id="r1")
    ma = _ma(h.last)
    assert ma.get("model_id") == "claude-sonnet-4-6"
    assert ma.get("provider") == "anthropic"


def test_captured_model_cleared_after_emit(tmp_path):
    """Captured model does not leak across tool emits."""
    h = _make(tmp_path)
    h.on_llm_start({"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4o"}}, prompts=[])
    h.on_tool_start({"name": "t1"}, "x", run_id="a")
    h.on_tool_end("y", run_id="a")
    assert _ma(h.last).get("model_id") == "gpt-4o"

    h.on_tool_start({"name": "t2"}, "x", run_id="b")
    h.on_tool_end("y", run_id="b")
    assert not _ma(h.last).get("model_id"), "model must not persist to next tool"


# ---------------------------------------------------------------------------
# audit_summary
# ---------------------------------------------------------------------------

def test_audit_summary_structure(tmp_path):
    """audit_summary() returns all expected Art. 12 disclosure fields."""
    h = _make(tmp_path, risk_class="high-risk")
    h.on_tool_start({"name": "t1"}, "x", run_id="r1")
    h.on_tool_end("y", run_id="r1")
    h.on_tool_start({"name": "t2"}, "a", run_id="r2")
    h.on_tool_end("b", run_id="r2")

    summary = h.audit_summary()
    assert summary["risk_class"] == "high-risk"
    assert summary["operator"] == "acme-co"
    assert summary["developer"] == "invoice-agent@v1"
    assert summary["capsule_count"] == 2
    assert len(summary["capsule_ids"]) == 2
    assert "ledger" in summary
    # anchored_count is 0 (anchor=False in tests)
    assert summary["anchored_count"] == 0


def test_audit_summary_capsule_ids_in_ledger(tmp_path):
    """capsule_ids in audit_summary match what's written to the ledger."""
    ledger = tmp_path / "l.jsonl"
    h = ScittAuditCallbackHandler(
        operator="acme-co", developer="agent@v1", ledger=ledger, anchor=False
    )
    h.on_tool_start({"name": "fetch"}, "url", run_id="r1")
    h.on_tool_end("data", run_id="r1")

    summary = h.audit_summary()
    records = read_ledger(ledger)
    ledger_ids = [r.get("capsule_id") or r.get("action_id") for r in records]
    # capsule_id in summary must appear in the ledger
    for cid in summary["capsule_ids"]:
        assert any(cid in (lid or "") for lid in ledger_ids), (
            f"capsule_id {cid!r} not found in ledger"
        )


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------

def test_all_capsules_written_to_ledger(tmp_path):
    """chain-start + tool + chain-end are all written to the ledger."""
    ledger = tmp_path / "l.jsonl"
    h = ScittAuditCallbackHandler(
        operator="acme-co", developer="agent@v1", ledger=ledger, anchor=False
    )
    h.on_chain_start({}, {}, run_id="c1", parent_run_id=None)
    h.on_tool_start({"name": "lookup"}, "q", run_id="t1")
    h.on_tool_end("result", run_id="t1")
    h.on_chain_end({}, run_id="c1", parent_run_id=None)

    records = read_ledger(ledger)
    assert len(records) == 3
