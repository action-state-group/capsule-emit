# SPDX-License-Identifier: Apache-2.0
"""The behaviours agent-action-capsule's deprecated CrewAI and LangChain
integrations were tested for (tests/test_emit_crewai.py and
tests/test_emit_langchain.py at f154515, #281), held by capsule-emit's own
adapters, which replace them.

Not carried over, because the adapters differ by design: AAC's ``emit_tool``
decorator (``CrewAICapsuleEmitter.wrap`` replaces it), its in-memory
``capsules`` list and its ``chain`` switch (capsule-emit records every capsule
in its ledger; ``results`` holds the returned ``EmitResult`` objects).
"""
from __future__ import annotations

import uuid

import pytest
from agent_action_capsule import verify

from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

OPERATOR = "ACME-CO"
DEVELOPER = "test-agent@v1"


def _crewai(tmp_path) -> CrewAICapsuleEmitter:
    return CrewAICapsuleEmitter(operator=OPERATOR, developer=DEVELOPER, ledger=tmp_path / "l.jsonl", anchor=False)


def test_crewai_wrap_emits_a_capsule_and_returns_the_value(tmp_path):
    emitter = _crewai(tmp_path)
    wrapped = emitter.wrap(lambda payload: {"total": payload.get("amount", 0) * 2}, action="invoice")
    assert wrapped({"amount": 50}) == {"total": 100}
    assert len(emitter.results) == 1


def test_crewai_wrapped_capsule_verifies_and_is_stamped(tmp_path):
    emitter = _crewai(tmp_path)

    def create_invoice(x: str) -> str:
        return x.upper()

    emitter.wrap(create_invoice)("hello")
    capsule = emitter.last.capsule
    assert verify(capsule).ok
    assert capsule["operator"] == OPERATOR and capsule["developer"] == DEVELOPER
    assert capsule["action_id"].startswith("create_invoice/")


def test_crewai_each_call_is_its_own_capsule(tmp_path):
    emitter = _crewai(tmp_path)
    emitter.wrap(lambda x: x, action="t1")("a")
    emitter.wrap(lambda x: x, action="t2")("b")
    first, second = emitter.results
    assert first.capsule_id != second.capsule_id
    assert all(verify(r.capsule).ok for r in emitter.results)


def _langchain(tmp_path):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    return LangChainCapsuleEmitter(operator=OPERATOR, developer=DEVELOPER, ledger=tmp_path / "l.jsonl", anchor=False)


def test_langchain_emits_a_verifying_capsule_on_tool_end(tmp_path):
    emitter = _langchain(tmp_path)
    run = uuid.uuid4()
    emitter.on_tool_start({"name": "search"}, "query", run_id=run)
    emitter.on_tool_end("result", run_id=run)
    capsule = emitter.last.capsule
    assert verify(capsule).ok
    assert capsule["operator"] == OPERATOR and capsule["developer"] == DEVELOPER
    assert capsule["disposition"]["decision"] == "accept"


def test_langchain_tool_error_emits_no_capsule(tmp_path):
    emitter = _langchain(tmp_path)
    run = uuid.uuid4()
    emitter.on_tool_start({"name": "search"}, "query", run_id=run)
    emitter.on_tool_error(RuntimeError("boom"), run_id=run)
    assert emitter.last is None


def test_langchain_concurrent_runs_keep_their_own_inputs(tmp_path):
    emitter = _langchain(tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    emitter.on_tool_start({"name": "tool_a"}, "in-a", run_id=a)
    emitter.on_tool_start({"name": "tool_b"}, "in-b", run_id=b)
    emitter.on_tool_end("out-b", run_id=b)
    emitter.on_tool_end("out-a", run_id=a)
    names = [r.capsule["action_id"].split("/")[0] for r in emitter.results]
    assert names == ["tool_b", "tool_a"]
