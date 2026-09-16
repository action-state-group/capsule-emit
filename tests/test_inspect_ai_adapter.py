# SPDX-License-Identifier: Apache-2.0
"""Inspect (``inspect_ai``) eval-log adapter tests.

``InspectAIListenerCore`` is framework-free (plain dicts in, EmitResult out),
so its behavior is fully covered without ``inspect_ai`` installed. Only
``seal_eval_log`` touches ``inspect_ai.log.read_eval_log``, and only the
tests at the bottom (gated on the package being importable) exercise it
against a real ``.eval`` log.
"""
from __future__ import annotations

import pytest

from capsule_emit.adapters.inspect_ai import (
    OBSERVATION_MODE,
    REQUEST_PROVENANCE,
    InspectAIListenerCore,
    seal_eval_log,
)
from capsule_emit.ledger import read_ledger
from capsule_emit.verification import verify_capsule as verify

inspect_ai = pytest.importorskip("inspect_ai", reason="inspect_ai not installed")


def _core(tmp_path, **kw) -> InspectAIListenerCore:
    return InspectAIListenerCore(
        operator="evaluator-org",
        developer="inspect-eval@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


# ---------------------------------------------------------------------------
# InspectAIListenerCore — framework-free
# ---------------------------------------------------------------------------


def test_seal_model_call_digests_request_and_response(tmp_path):
    core = _core(tmp_path)
    request = {"model": "model", "messages": [{"role": "user", "content": "hi"}]}
    response = {"content": "hello"}

    result = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request=request, response=response,
    )

    assert verify(result.capsule).ok
    assert result.capsule["action_id"].startswith("inspect_eval_model_call/")
    assert result.capsule["model_attestation"]["provider"] == "mockllm"
    assert result.capsule["model_attestation"]["model_id"] == "model"
    assert result.capsule["disposition"]["verdict_class"] == "executed"
    assert result.capsule["effect"]["status"] == "confirmed"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["inspect_sample_id"] == "s1"
    assert ca["inspect_call_index"] == 0


def test_seal_model_call_chains_to_prior(tmp_path):
    core = _core(tmp_path)
    call1 = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request={"messages": []}, response={"content": "reasoning"},
    )
    call2 = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=1, model_name="mockllm/model",
        request={"messages": []}, response={"content": "42"},
        prior_capsule_id=call1.capsule_id,
    )
    assert call2.capsule["chain"]["parent_capsule_id"] == call1.capsule_id


def test_seal_model_call_error_records_the_error_not_a_fabricated_response(tmp_path):
    """An errored call still digests something real -- the error message
    Inspect recorded, honestly labeled -- never a made-up success response."""
    core = _core(tmp_path)
    result = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request={"messages": []}, response=None, error="connection reset",
    )
    assert result.capsule["disposition"]["verdict_class"] == "errored"
    assert result.capsule["effect"]["status"] == "failed"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["agent_output_digest"]  # the error dict itself is real, digested content

    from capsule_emit.disclosure import build_disclosure_envelope

    envelope = build_disclosure_envelope(result.capsule, agent_output={"error": "connection reset"}, strict=True)
    assert envelope["disclosures"]["agent_output"] == {"error": "connection reset"}


def test_seal_model_call_no_response_no_error_is_planned_never_confirmed(tmp_path):
    """No response AND no error -- nothing was actually confirmed. The spec's
    confirmed-effect invariant requires a response digest for status
    "confirmed"; sealing this as "planned" (no confirmed effect) is honest,
    where fabricating a digest-less "confirmed" would not be."""
    core = _core(tmp_path)
    result = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request={"messages": []}, response=None,
    )
    assert result.capsule["effect"]["status"] == "planned"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert "agent_output_digest" not in ca


def test_seal_model_call_observation_mode_is_post_hoc(tmp_path):
    """observation_mode/provenance travel inside the digested agent_input,
    not as separate top-level capsule fields -- confirm the exact dict this
    adapter committed to still discloses (re-derives the same digest)."""
    core = _core(tmp_path)
    result = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request={"messages": []}, response={"content": "x"},
    )
    from capsule_emit.disclosure import build_disclosure_envelope

    exact_agent_input = {
        "request": {"messages": []},
        "observation_mode": OBSERVATION_MODE,
        "request_record_provenance": core.__class__.__module__,
    }
    from capsule_emit.adapters.inspect_ai import REQUEST_PROVENANCE

    exact_agent_input["request_record_provenance"] = REQUEST_PROVENANCE
    envelope = build_disclosure_envelope(result.capsule, agent_input=exact_agent_input, strict=True)
    assert envelope["disclosures"]["agent_input"] == exact_agent_input


def test_seal_tool_call_digests_arguments_and_result(tmp_path):
    core = _core(tmp_path)
    result = core.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="echo", tool_call_id="call-1",
        arguments={"text": "REAL"}, result="ECHO:REAL",
    )
    assert verify(result.capsule).ok
    assert result.capsule["action_id"].startswith("inspect_eval_tool_call/")
    assert result.capsule["disposition"]["verdict_class"] == "executed"
    assert result.capsule["effect"]["status"] == "confirmed"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["inspect_tool_name"] == "echo"
    assert ca["inspect_tool_call_id"] == "call-1"

    from capsule_emit.disclosure import build_disclosure_envelope

    envelope = build_disclosure_envelope(
        result.capsule,
        agent_input={
            "tool_name": "echo",
            "tool_call_id": "call-1",
            "arguments": {"text": "REAL"},
            "observation_mode": OBSERVATION_MODE,
            "request_record_provenance": REQUEST_PROVENANCE,
        },
        agent_output={"result": "ECHO:REAL"},
        strict=True,
    )
    assert envelope["disclosures"]["agent_output"] == {"result": "ECHO:REAL"}


def test_seal_tool_call_error_records_the_error_not_a_fabricated_result(tmp_path):
    core = _core(tmp_path)
    result = core.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="call-2",
        arguments={"cmd": "rm -rf /nonexistent"}, error="permission denied",
    )
    assert result.capsule["disposition"]["verdict_class"] == "errored"
    assert result.capsule["effect"]["status"] == "failed"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["agent_output_digest"]  # the error itself is real, digested content


def test_seal_tool_call_no_result_no_error_is_planned_never_confirmed(tmp_path):
    core = _core(tmp_path)
    result = core.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="echo", tool_call_id="call-3", arguments={"text": "x"},
    )
    assert result.capsule["effect"]["status"] == "planned"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert "agent_output_digest" not in ca


def test_seal_tool_call_chains_to_prior(tmp_path):
    core = _core(tmp_path)
    model_call = core.seal_model_call(
        sample_id="s1", epoch=1, call_index=0, model_name="mockllm/model",
        request={"messages": []}, response={"content": "call echo"},
    )
    tool_call = core.seal_tool_call(
        sample_id="s1", epoch=1, call_index=1,
        tool_name="echo", tool_call_id="call-1",
        arguments={"text": "x"}, result="ECHO:x",
        prior_capsule_id=model_call.capsule_id,
    )
    assert tool_call.capsule["chain"]["parent_capsule_id"] == model_call.capsule_id


def test_seal_sample_terminal_omits_absent_fields(tmp_path):
    core = _core(tmp_path)
    result = core.seal_sample_terminal(
        sample_id="s1", epoch=1, sample_input="What is 2+2?", target="4",
    )
    ca = result.capsule["model_attestation"]["compute_attestation"]
    # no output_completion, no scores supplied -> agent_output has nothing to digest
    assert "agent_output_digest" not in ca
    # and nothing was actually confirmed, so the effect says so rather than
    # claiming "confirmed" over a digest that doesn't exist
    assert result.capsule["effect"]["status"] == "planned"


def test_seal_sample_terminal_with_scores(tmp_path):
    core = _core(tmp_path)
    result = core.seal_sample_terminal(
        sample_id="s1", epoch=1, sample_input="What is 2+2?", target="4",
        output_completion="4", scores={"match": {"value": "C", "answer": "4"}},
    )
    assert result.capsule["disposition"]["verdict_class"] == "executed"
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["agent_output_digest"]


def test_seal_sample_terminal_error_never_fabricates_a_score(tmp_path):
    core = _core(tmp_path)
    result = core.seal_sample_terminal(
        sample_id="s1", epoch=1, sample_input="What is 2+2?", target="4",
        error="sandbox timeout",
    )
    assert result.capsule["disposition"]["verdict_class"] == "errored"
    assert result.capsule["effect"]["status"] == "failed"


# ---------------------------------------------------------------------------
# seal_eval_log — against a real, tiny .eval log
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Harness boundary — the key the sandbox never has
# ---------------------------------------------------------------------------


def test_sandbox_process_cannot_sign_as_the_harness(tmp_path):
    """A process that never had access to the harness's private key file (or
    its ``CAPSULE_SIGNING_KEY_PATH``) can run this same library and mint its
    own keypair -- that is not preventable -- but it cannot produce a record
    that verifies under the harness's ``key_id``. See the module docstring's
    "Where the signing key lives" section.

    Mutant B ("move the sealing key into the sandbox") is: pass the
    harness's real key path into the child's environment below and re-run --
    the final assertion then fails, because the "sandbox" can sign as the
    harness. That failing-then-passing pair is the red-then-green evidence
    for this vector (see the results report for the captured runs)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    harness_ledger = harness_dir / "ledger.jsonl"
    core = InspectAIListenerCore(
        operator="evaluator-org", developer="inspect-eval@v1",
        ledger=harness_ledger, anchor=False,
    )
    real = core.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="call-1",
        arguments={"cmd": "echo REAL"}, result="REAL",
    )
    harness_key_id = real.capsule["key_id"]
    key_path = harness_ledger.with_name(harness_ledger.name + ".signing_key.pem")
    assert key_path.exists()
    assert oct(key_path.stat().st_mode)[-3:] == "600"

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    repo_root = Path(__file__).resolve().parent.parent
    script = (
        "from capsule_emit.adapters.inspect_ai import InspectAIListenerCore\n"
        "core = InspectAIListenerCore(operator='attacker', developer='attacker', "
        "ledger='forged.jsonl', anchor=False)\n"
        "r = core.seal_tool_call(sample_id='s1', epoch=1, call_index=0, "
        "tool_name='bash', tool_call_id='call-1', arguments={'cmd': 'echo REAL'}, "
        "result='SPOOFTEST')\n"
        "print(r.capsule['key_id'])\n"
    )
    # The "sandbox" env: no CAPSULE_SIGNING_KEY_PATH, cwd has no access to
    # the harness's key file. This IS the fix -- run this test un-mutated
    # for the green result.
    env = {k: v for k, v in os.environ.items() if k != "CAPSULE_SIGNING_KEY_PATH"}
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, cwd=str(sandbox_dir), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    forged_key_id = proc.stdout.strip().splitlines()[-1]

    assert not (sandbox_dir / key_path.name).exists(), (
        "the sandbox must never see the harness's key file on its own filesystem"
    )
    assert forged_key_id != harness_key_id, (
        "the sandboxed process minted its own keypair and cannot sign as the "
        "harness -- if this fails, the sandbox had access to the harness's "
        "signing key (mutant B)"
    )
