# SPDX-License-Identifier: Apache-2.0
"""``seal_eval_log`` tests against a real, tiny Inspect ``.eval`` log.

These need ``inspect_ai`` (the ``inspect-ai`` extra) and are skipped when it
is not installed. CI runs them in the dedicated ``inspect-ai`` job in
``.github/workflows/python.yml``. The framework-free core tests are in
``test_inspect_ai_core.py`` and run without the package.
"""
from __future__ import annotations

import pytest

from capsule_emit.adapters.inspect_ai import seal_eval_log
from capsule_emit.ledger import read_ledger
from capsule_emit.verification import verify_capsule as verify

inspect_ai = pytest.importorskip("inspect_ai", reason="inspect_ai not installed")


def _mock_constant_answer(input, tools, tool_choice, config):
    from inspect_ai.model import ModelOutput

    return ModelOutput.from_content(model="mockllm/model", content="4")


@pytest.fixture(scope="module")
def _tiny_task():
    from inspect_ai import Task, task
    from inspect_ai.dataset import Sample
    from inspect_ai.scorer import match

    @task
    def tiny_addition():
        return Task(
            dataset=[Sample(input="What is 2 + 2?", target="4", id="add-2-2")],
            scorer=match(),
        )

    return tiny_addition


def _make_eval_log(tmp_path, tiny_task):
    from inspect_ai import eval as inspect_eval

    log_dir = tmp_path / "logs"
    logs = inspect_eval(
        tiny_task(),
        model="mockllm/model",
        model_args={"custom_outputs": _mock_constant_answer},
        log_dir=str(log_dir),
        log_format="eval",
        display="none",
    )
    return logs[0].location


def test_seal_eval_log_one_record_per_call_and_terminal(tmp_path, _tiny_task):
    eval_log_path = _make_eval_log(tmp_path, _tiny_task)
    ledger = tmp_path / "ledger.jsonl"

    results = seal_eval_log(
        eval_log_path,
        operator="evaluator-org",
        developer="inspect-eval@v1",
        ledger=str(ledger),
        anchor=False,
    )

    assert len(results) == 1
    sample_results = next(iter(results.values()))
    # exactly one generate() call + one terminal record
    assert len(sample_results) == 2

    records = read_ledger(ledger)
    assert len(records) == 2
    for r in records:
        assert verify(r).ok
    # terminal record chains to the model-call record
    assert records[1]["chain"]["parent_capsule_id"] == records[0]["capsule_id"]
    assert records[0]["action_id"].startswith("inspect_eval_model_call/")
    assert records[1]["action_id"].startswith("inspect_eval_sample_result/")


def test_seal_eval_log_terminal_carries_target_and_score(tmp_path, _tiny_task):
    eval_log_path = _make_eval_log(tmp_path, _tiny_task)
    ledger = tmp_path / "ledger.jsonl"
    seal_eval_log(
        eval_log_path, operator="evaluator-org", developer="inspect-eval@v1",
        ledger=str(ledger), anchor=False,
    )
    records = read_ledger(ledger)
    terminal = records[-1]
    ca = terminal["model_attestation"]["compute_attestation"]
    assert ca["agent_input_digest"]  # sample input+target were digested
    assert ca.get("agent_output_digest")  # completion+scores were digested


@pytest.fixture(scope="module")
def _tool_calling_task():
    """A model call, a tool call, a model call, in one sample -- the mixed
    event-type sequence ``seal_eval_log`` must chain in Inspect's own order,
    not as two separate chains."""
    from inspect_ai import Task, task
    from inspect_ai.dataset import Sample
    from inspect_ai.scorer import match
    from inspect_ai.solver import solver, use_tools
    from inspect_ai.tool import tool

    @tool
    def echo():
        async def execute(text: str):
            """Echo the given text back.

            Args:
                text: text to echo
            """
            return f"ECHO:{text}"

        return execute

    @solver
    def call_tool_then_answer():
        async def solve(state, generate):
            state = await generate(state, tool_calls="single")
            state = await generate(state)
            return state

        return solve

    @task
    def tool_task() -> Task:
        return Task(
            dataset=[Sample(input="echo REAL then answer", target="done", id="echo-1")],
            solver=[use_tools(echo()), call_tool_then_answer()],
            scorer=match(),
        )

    return tool_task


def _mock_tool_then_answer(input, tools, tool_choice, config):
    from inspect_ai.model import ModelOutput

    has_tool_result = any(getattr(m, "role", None) == "tool" for m in input)
    if has_tool_result:
        return ModelOutput.from_content(model="mockllm/model", content="done")
    return ModelOutput.for_tool_call(
        model="mockllm/model", tool_name="echo", tool_arguments={"text": "REAL"}
    )


def test_seal_eval_log_chains_model_and_tool_events_in_order(tmp_path, _tool_calling_task):
    from inspect_ai import eval as inspect_eval

    log_dir = tmp_path / "logs"
    logs = inspect_eval(
        _tool_calling_task(),
        model="mockllm/model",
        model_args={"custom_outputs": _mock_tool_then_answer},
        log_dir=str(log_dir),
        log_format="eval",
        display="none",
    )
    eval_log_path = logs[0].location
    ledger = tmp_path / "ledger.jsonl"

    results = seal_eval_log(
        eval_log_path, operator="evaluator-org", developer="inspect-eval@v1",
        ledger=str(ledger), anchor=False,
    )
    sample_results = next(iter(results.values()))
    # model call, tool call, model call, terminal
    assert len(sample_results) == 4

    records = read_ledger(ledger)
    assert len(records) == 4
    for r in records:
        assert verify(r).ok
    assert records[0]["action_id"].startswith("inspect_eval_model_call/")
    assert records[1]["action_id"].startswith("inspect_eval_tool_call/")
    assert records[2]["action_id"].startswith("inspect_eval_model_call/")
    assert records[3]["action_id"].startswith("inspect_eval_sample_result/")
    # chained end-to-end, in Inspect's own event order -- not two separate chains
    assert records[1]["chain"]["parent_capsule_id"] == records[0]["capsule_id"]
    assert records[2]["chain"]["parent_capsule_id"] == records[1]["capsule_id"]
    assert records[3]["chain"]["parent_capsule_id"] == records[2]["capsule_id"]

    tool_ca = records[1]["model_attestation"]["compute_attestation"]
    assert tool_ca["inspect_tool_name"] == "echo"
    assert records[1]["effect"]["status"] == "confirmed"
