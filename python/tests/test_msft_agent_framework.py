# SPDX-License-Identifier: Apache-2.0
"""Microsoft Agent Framework middleware tests — framework-free core + real-SDK shells.

Sealing logic lives in AgentFrameworkCore, whose methods take duck-typed context
objects, so the full behavior is exercised WITHOUT agent-framework installed (mirrors
the CrewAI/LangChain/agno/Strands listener test approach). The two middleware shells
and the end-to-end agent loop are covered in ``test_msft_agent_framework_sdk.py``,
which drives a REAL ``agent_framework.Agent`` against a scripted, keyless chat client.

Covered:
- planned capsule before the wrapped body (effect.status="planned"), verifies
- confirmed capsule after it, confirms-chained to the planned id
- tool exception → verdict="errored", status="failed", chained
- another middleware's MiddlewareTermination/MiddlewareFailure → verdict="blocked",
  effect left "planned", chained, with the unobservability markers
- run seam: action "<agent name>.run", model captured from agent.client
- model attribution flows run → tool call through a ContextVar, and two agents on
  different models running concurrently do not cross-attribute
- never-raise guarantee: a broken ledger warns and does not fail the agent run —
  which at the function seam would silently become a tool-error result the model reads
- digest-only privacy: raw argument/output values never reach the ledger
- payload projection: bytes omitted, pydantic models dumped, to_dict objects flattened,
  cycles depth-capped
- float args fail closed (documents CURRENT main behavior; see #135)
- shell: the two objects are routed to their own seams by the framework's own
  categorize_middleware, and Agent(middleware=[...]) accepts them
- real SDK: full agent loop e2e for the clean, error, terminate and fail-closed paths,
  a broken emitter that does not take the agent down, and the shipped demo
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from capsule_emit.adapters.msft_agent_framework import (
    _RUN_MODEL,
    AgentFrameworkCore,
    _is_control_flow,
    _jsonable,
    _messages_payload,
    _model_from_agent,
)
from capsule_emit.verification import verify_capsule as verify

ARGS = {"po": "PO-1", "amount": "120.00"}


# ---------------------------------------------------------------------------
# Helpers — duck-typed stand-ins for the framework's context objects
# ---------------------------------------------------------------------------


class _Fn:
    def __init__(self, name="submit_order"):
        self.name = name


class _FnCtx:
    """Stand-in for FunctionInvocationContext (attribute access only)."""

    def __init__(self, name="submit_order", arguments=None, result=None):
        self.function = _Fn(name)
        self.arguments = ARGS if arguments is None else arguments
        self.result = result
        self.session = None
        self.metadata = {}
        self.kwargs = {}
        self.tools = None


class _Client:
    OTEL_PROVIDER_NAME = "scripted"

    def __init__(self, model="scripted-demo-model"):
        self.model = model


class _Agent:
    def __init__(self, name="procurement", client=None):
        self.name = name
        self.client = client if client is not None else _Client()


class _AgentCtx:
    """Stand-in for AgentContext (attribute access only)."""

    def __init__(self, agent=None, messages=None, result=None, stream=False):
        self.agent = agent if agent is not None else _Agent()
        self.messages = messages if messages is not None else []
        self.result = result
        self.stream = stream
        self.metadata = {}


class _Msg:
    def __init__(self, role, text):
        self.role = role
        self.text = text


def _core(tmp_path, **kw) -> AgentFrameworkCore:
    return AgentFrameworkCore(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ledger(tmp_path) -> list[dict]:
    path = Path(tmp_path) / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Core — the function seam
# ---------------------------------------------------------------------------


def test_function_planned_seals_a_verifying_commitment_record(tmp_path):
    core = _core(tmp_path)
    planned_id = core.function_planned(_FnCtx())

    caps = _ledger(tmp_path)
    assert len(caps) == 1
    cap = caps[0]
    assert cap["capsule_id"] == planned_id
    assert cap["action_id"].startswith("submit_order/")
    assert cap["effect"]["status"] == "planned"
    assert cap["disposition"]["verdict_class"] == "executed"
    assert cap.get("chain", {}).get("parent_capsule_id") is None
    assert verify(cap).ok


def test_function_confirmed_is_chained_to_its_planned_capsule(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx()
    planned_id = core.function_planned(ctx)
    ctx.result = "12.00 USD"
    core.function_confirmed(ctx, planned_id)

    planned, confirmed = _ledger(tmp_path)
    assert confirmed["effect"]["status"] == "confirmed"
    assert confirmed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert confirmed["chain"]["relation"] == "confirms"
    assert verify(confirmed).ok


def test_function_failure_is_errored_and_failed_and_chained(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx()
    planned_id = core.function_planned(ctx)
    core.function_failed(ctx, planned_id, RuntimeError("order system rejected PO-1"))

    planned, failed = _ledger(tmp_path)
    assert failed["disposition"]["verdict_class"] == "errored"
    assert failed["effect"]["status"] == "failed"
    assert failed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert verify(failed).ok


@pytest.mark.parametrize("exc_name", ["MiddlewareTermination", "MiddlewareFailure"])
def test_function_block_records_someone_elses_refusal_without_claiming_an_effect(tmp_path, exc_name):
    core = _core(tmp_path)
    ctx = _FnCtx()
    planned_id = core.function_planned(ctx)
    exc = type(exc_name, (Exception,), {})("denied by policy")
    core.function_blocked(ctx, planned_id, exc)

    _planned, blocked = _ledger(tmp_path)
    assert blocked["disposition"]["verdict_class"] == "blocked"
    # NOT "dispatched": whether the wrapped body ran is unobservable from this seam,
    # and an unknown status would derive effect_mode "dispatched_unconfirmed".
    assert blocked["effect"]["status"] == "planned"
    compute = blocked["model_attestation"]["compute_attestation"]
    assert compute["agent_framework_blocked_by"] == exc_name
    assert compute["agent_framework_effect_unobservable"] is True
    assert compute["agent_framework_result_present"] is False
    assert verify(blocked).ok


def test_block_records_whether_a_result_was_already_present(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx(result="a substituted cache hit")
    exc = type("MiddlewareTermination", (Exception,), {})("cached")
    core.function_blocked(ctx, None, exc)

    compute = _ledger(tmp_path)[0]["model_attestation"]["compute_attestation"]
    assert compute["agent_framework_result_present"] is True


def test_every_capsule_is_stamped_observation_only(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx()
    planned_id = core.function_planned(ctx)
    core.function_confirmed(ctx, planned_id)
    run_id, model = core.run_planned(_AgentCtx())
    core.run_confirmed(_AgentCtx(), run_id, model)

    for cap in _ledger(tmp_path):
        compute = cap["model_attestation"]["compute_attestation"]
        assert compute["observation_mode"] == "in_path_wrapper"
        assert compute["runtime"] == "msft-agent-framework"


def test_unnamed_function_falls_back_rather_than_guessing(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx()
    ctx.function = None
    core.function_planned(ctx)
    assert _ledger(tmp_path)[0]["action_id"].startswith("function/")


# ---------------------------------------------------------------------------
# Core — the run seam
# ---------------------------------------------------------------------------


def test_run_seam_names_the_agent_and_captures_its_model(tmp_path):
    core = _core(tmp_path)
    ctx = _AgentCtx(messages=[_Msg("user", "price for SKU-1?")])
    planned_id, model = core.run_planned(ctx)
    ctx.result = type("Resp", (), {"text": "SKU-1 is 12.00 USD."})()
    core.run_confirmed(ctx, planned_id, model)

    planned, confirmed = _ledger(tmp_path)
    assert planned["action_id"].startswith("procurement.run/")
    assert model == {"provider": "scripted", "model_id": "scripted-demo-model"}
    assert planned["model_attestation"]["provider"] == "scripted"
    assert planned["model_attestation"]["model_id"] == "scripted-demo-model"
    assert confirmed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert confirmed["effect"]["status"] == "confirmed"
    assert verify(planned).ok and verify(confirmed).ok


def test_anonymous_agent_uses_a_generic_run_action(tmp_path):
    core = _core(tmp_path)
    core.run_planned(_AgentCtx(agent=_Agent(name=None)))
    assert _ledger(tmp_path)[0]["action_id"].startswith("agent.run/")


def test_streaming_run_is_marked_so_nobody_reads_it_as_a_finished_transcript(tmp_path):
    core = _core(tmp_path)
    ctx = _AgentCtx(stream=True)
    planned_id, model = core.run_planned(ctx)
    # A ResponseStream is not a finished AgentResponse and has no .text yet.
    ctx.result = type("Stream", (), {})()
    core.run_confirmed(ctx, planned_id, model)

    for cap in _ledger(tmp_path):
        assert cap["model_attestation"]["compute_attestation"]["agent_framework_stream"] is True


def test_run_failure_and_block_mirror_the_function_seam(tmp_path):
    core = _core(tmp_path)
    ctx = _AgentCtx()
    planned_id, model = core.run_planned(ctx)
    core.run_failed(ctx, planned_id, model, RuntimeError("client exploded"))
    core.run_blocked(ctx, planned_id, model, type("MiddlewareFailure", (Exception,), {})("denied"))

    _planned, failed, blocked = _ledger(tmp_path)
    assert (failed["disposition"]["verdict_class"], failed["effect"]["status"]) == ("errored", "failed")
    assert (blocked["disposition"]["verdict_class"], blocked["effect"]["status"]) == ("blocked", "planned")
    assert blocked["model_attestation"]["compute_attestation"]["agent_framework_seam"] == "agent"


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_model_from_agent_is_none_when_there_is_nothing_to_read():
    assert _model_from_agent(None) is None
    assert _model_from_agent(_Agent(client=object())) is None


def test_model_from_agent_does_not_report_the_base_class_placeholder_as_a_provider():
    class Bare:
        # BaseChatClient.OTEL_PROVIDER_NAME defaults to "unknown" (_clients.py:271).
        OTEL_PROVIDER_NAME = "unknown"
        model = "some-model"

    assert _model_from_agent(_Agent(client=Bare())) == {
        "provider": "unknown",
        "model_id": "some-model",
    }


def test_function_capsules_carry_the_running_agents_model(tmp_path):
    core = _core(tmp_path)
    token = _RUN_MODEL.set({"provider": "scripted", "model_id": "scripted-demo-model"})
    try:
        core.function_planned(_FnCtx())
    finally:
        _RUN_MODEL.reset(token)

    attestation = _ledger(tmp_path)[0]["model_attestation"]
    assert attestation["provider"] == "scripted"
    assert attestation["model_id"] == "scripted-demo-model"


def test_without_a_run_seam_the_function_model_falls_back_to_config_never_a_guess(tmp_path):
    core = _core(tmp_path, model={"provider": "configured", "model_id": "cfg-1"})
    core.function_planned(_FnCtx())
    assert _ledger(tmp_path)[0]["model_attestation"]["provider"] == "configured"


# ---------------------------------------------------------------------------
# Never-raises
# ---------------------------------------------------------------------------


def test_a_broken_ledger_warns_and_never_reaches_the_agent(tmp_path):
    # A ledger path whose parent is an existing FILE cannot be opened or
    # created. (An empty directory at the path is not broken: the ledger
    # creates its store there, ledger_store.create_store.)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    core = AgentFrameworkCore(
        operator="acme-co", developer="my-agent@v1", ledger=blocker / "ledger.jsonl", anchor=False
    )
    with pytest.warns(RuntimeWarning, match="failed to seal"):
        assert core.function_planned(_FnCtx()) is None
    with pytest.warns(RuntimeWarning, match="failed to seal"):
        core.function_confirmed(_FnCtx(), None)


def test_float_arguments_chain_as_of_070(tmp_path):
    # As of 0.7.0 (#135) floats canonicalize to RFC 8785 decimal strings before
    # digesting, so a float argument seals a normal planned capsule. Pre-0.7.0
    # this warned and dropped the capsule.
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = core.function_planned(_FnCtx(arguments={"amount": 120.0}))
    assert result is not None
    assert not any("failed to seal" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 1 and caps[0]["effect"]["status"] == "planned"


# ---------------------------------------------------------------------------
# Privacy and payload projection
# ---------------------------------------------------------------------------


def test_raw_arguments_and_outputs_never_reach_the_ledger(tmp_path):
    core = _core(tmp_path)
    ctx = _FnCtx(arguments={"po": "PO-SECRET", "note": "customer bank detail"})
    planned_id = core.function_planned(ctx)
    ctx.result = "acct 1234-5678"
    core.function_confirmed(ctx, planned_id)

    raw = (Path(tmp_path) / "ledger.jsonl").read_text()
    assert "PO-SECRET" not in raw
    assert "customer bank detail" not in raw
    assert "1234-5678" not in raw
    for cap in _ledger(tmp_path):
        # The only trace of the payload is a digest inside the compute attestation.
        compute = cap["model_attestation"]["compute_attestation"]
        digest = compute.get("agent_input_digest") or compute.get("agent_output_digest")
        assert digest and len(digest) == 64


def test_jsonable_omits_bytes_rather_than_failing_the_digest():
    assert _jsonable(b"\x00\x01\x02") == "<omitted:3 bytes>"
    assert _jsonable({"blob": bytearray(4)}) == {"blob": "<omitted:4 bytes>"}


def test_jsonable_dumps_pydantic_models_and_to_dict_objects():
    class Model:
        def model_dump(self, mode=None):
            return {"sku": "SKU-1", "mode": mode}

    class Content:
        def to_dict(self):
            return {"type": "text", "text": "5"}

    assert _jsonable(Model()) == {"sku": "SKU-1", "mode": "json"}
    assert _jsonable([Content()]) == [{"type": "text", "text": "5"}]


def test_jsonable_survives_a_self_referential_payload():
    loop: dict = {}
    loop["self"] = loop
    # Depth-capped rather than recursing forever: sealing must never hang a tool call.
    assert isinstance(json.dumps(_jsonable(loop)), str)


def test_jsonable_passes_floats_through_so_the_digest_stays_the_one_that_fails_closed():
    assert _jsonable({"amount": 12.5}) == {"amount": 12.5}


def test_messages_are_projected_to_role_and_text():
    assert _messages_payload([_Msg("user", "hi"), _Msg("assistant", "hello")]) == [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "hello"},
    ]
    assert _messages_payload("not a list") == "not a list"


def test_control_flow_is_matched_by_name_so_the_core_needs_no_sdk():
    assert _is_control_flow(type("MiddlewareTermination", (Exception,), {})())
    assert _is_control_flow(type("MiddlewareFailure", (Exception,), {})())
    assert _is_control_flow(type("Sub", (type("MiddlewareFailure", (Exception,), {}),), {})())
    assert not _is_control_flow(RuntimeError("boom"))


def test_seal_runs_false_records_tool_calls_only(tmp_path):
    core = _core(tmp_path, seal_runs=False)
    assert core.seal_runs is False


def test_missing_sdk_points_at_the_extra(monkeypatch):
    import capsule_emit.adapters.msft_agent_framework as mod

    monkeypatch.setattr(mod, "_HAVE_AGENT_FRAMEWORK", False)
    with pytest.raises(ImportError, match=r"capsule-emit\[msft-agent-framework\]"):
        mod.CapsuleFunctionMiddleware(operator="o", developer="d")
    with pytest.raises(ImportError, match=r"capsule-emit\[msft-agent-framework\]"):
        mod.capsule_middleware(operator="o", developer="d")
