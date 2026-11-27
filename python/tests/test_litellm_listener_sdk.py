# SPDX-License-Identifier: Apache-2.0
"""LiteLLM listener tests against the real litellm wheel: the ``CustomLogger``
shell, the config-load path and hermetic end-to-end runs. The framework-free
core is tested in ``test_litellm_listener.py``.
"""
from __future__ import annotations

import asyncio

import pytest
from test_litellm_listener import (  # helpers shared with the framework-free tests
    RESPONSE,
    _compute,
    _core,
    _kwargs,
    _ledger,
    _request_data,
)

from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# The shell — needs the real litellm wheel
# ---------------------------------------------------------------------------

litellm = pytest.importorskip("litellm", reason="needs capsule-emit[litellm]")


def _listener(tmp_path, **kw):
    from capsule_emit.adapters.litellm_listener import LiteLLMCapsuleListener

    return LiteLLMCapsuleListener(
        operator="acme-co",
        developer="gateway@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def test_shell_is_a_real_custom_logger(tmp_path):
    from litellm.integrations.custom_logger import CustomLogger

    assert isinstance(_listener(tmp_path), CustomLogger)


def test_shell_does_not_override_the_deny_capable_pre_call_hook(tmp_path):
    """async_pre_call_hook is the gate layer. If this test ever fails, someone
    has given the record layer the power to reject a user's request."""
    from litellm.integrations.custom_logger import CustomLogger

    from capsule_emit.adapters.litellm_listener import LiteLLMCapsuleListener

    for deny_capable in (
        "async_pre_call_hook",
        "async_post_call_success_hook",
        "async_moderation_hook",
    ):
        assert getattr(LiteLLMCapsuleListener, deny_capable) is getattr(
            CustomLogger, deny_capable
        ), f"{deny_capable} must stay inherited — it can change what the caller gets"


def test_failure_hook_returns_none_so_the_client_error_is_untouched(tmp_path):
    """litellm/proxy/utils.py: the first callback to return or raise an
    HTTPException rewrites the error the client sees. Never us."""
    listener = _listener(tmp_path)
    out = asyncio.run(
        listener.async_post_call_failure_hook(
            request_data=_request_data(),
            original_exception=RuntimeError("upstream exploded"),
            user_api_key_dict=None,
            traceback_str="Traceback (most recent call last): ...",
        )
    )
    assert out is None
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "failed"]
    assert _compute(caps[1])["traceback_recorded"] is True


def test_shell_success_hook_seals_the_chain(tmp_path):
    listener = _listener(tmp_path)
    asyncio.run(listener.async_log_success_event(_kwargs(), RESPONSE, None, None))
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    assert all(verify(c).ok for c in caps)


# -- the config-load path, through litellm's own loader ---------------------


DOTTED = "capsule_emit.adapters.litellm_listener.proxy_handler_instance"


def test_get_instance_fn_resolves_the_dotted_path(tmp_path, monkeypatch):
    from litellm.integrations.custom_logger import CustomLogger
    from litellm.proxy.types_utils.utils import get_instance_fn

    monkeypatch.setenv("CAPSULE_EMIT_OPERATOR", "acme-co")
    monkeypatch.setenv("CAPSULE_EMIT_DEVELOPER", "gateway@v1")
    monkeypatch.setenv("CAPSULE_EMIT_LEDGER", str(tmp_path / "ledger.jsonl"))
    inst = get_instance_fn(DOTTED)
    assert isinstance(inst, CustomLogger)
    assert inst.core._operator == "acme-co"


def test_the_class_is_rejected_by_litellms_loader(monkeypatch):
    """Why ``proxy_handler_instance`` exists at all: litellm refuses a class."""
    from litellm.proxy.common_utils.callback_utils import _loaded_callback_or_raise

    from capsule_emit.adapters.litellm_listener import LiteLLMCapsuleListener

    with pytest.raises(ValueError) as exc:
        _loaded_callback_or_raise(entry="x.y.Z", loaded=LiteLLMCapsuleListener)
    assert "neither a CustomLogger instance nor a callable" in str(exc.value)


def test_initialize_callbacks_on_proxy_registers_us(tmp_path, monkeypatch):
    initialize = pytest.importorskip(
        "litellm.proxy.common_utils.callback_utils", reason="needs litellm[proxy]"
    )
    pytest.importorskip("websockets", reason="needs litellm[proxy]")
    monkeypatch.setenv("CAPSULE_EMIT_OPERATOR", "acme-co")
    monkeypatch.setenv("CAPSULE_EMIT_DEVELOPER", "gateway@v1")
    monkeypatch.setenv("CAPSULE_EMIT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(litellm, "callbacks", [], raising=False)
    initialize.initialize_callbacks_on_proxy(
        value=[DOTTED],
        premium_user=False,
        config_file_path=str(tmp_path / "config.yaml"),
        litellm_settings={},
    )
    assert "LiteLLMCapsuleListener" in [type(c).__name__ for c in litellm.callbacks]


# -- end to end against the real wheel, hermetic (mock_response, no key) ----


def _drive(callbacks, *, prompt="hello", mock="hi there"):
    async def run():
        litellm.callbacks = list(callbacks)
        litellm.success_callback = []
        litellm._async_success_callback = []
        out = await litellm.acompletion(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            mock_response=mock,
        )
        await asyncio.sleep(1.0)  # the success handler is scheduled, not awaited
        return out

    return asyncio.run(run())


def test_end_to_end_acompletion_seals_a_verifying_chain(tmp_path):
    listener = _listener(tmp_path)
    _drive([listener])
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)
    assert _compute(caps[0])["litellm_call_id"] == _compute(caps[1])["litellm_call_id"]


def test_we_digest_what_the_operators_redaction_produced(tmp_path):
    """The load-bearing documentation claim, measured against the real wheel:
    litellm runs EVERY callback's async_logging_hook to completion before it
    dispatches ANY async_log_success_event, and threads the return value in. So
    a sibling redactor's output is what we commit to — the redacted view."""
    from litellm.integrations.custom_logger import CustomLogger

    class Redactor(CustomLogger):
        async def async_logging_hook(self, kwargs, result, call_type):
            new_kwargs = dict(kwargs)
            new_kwargs["messages"] = [{"role": "user", "content": "[REDACTED]"}]
            return new_kwargs, result

    plain = _listener(tmp_path)
    _drive([plain], prompt="my SSN is 123-45-6789")
    redacted_dir = tmp_path / "r"
    redacted_dir.mkdir()
    guarded = _listener(redacted_dir)
    _drive([Redactor(), guarded], prompt="my SSN is 123-45-6789")

    # different preimages => different request digests
    a = _compute(_ledger(tmp_path)[0])["agent_input_digest"]
    b = _compute(_ledger(redacted_dir)[0])["agent_input_digest"]
    assert a != b

    # and the redacted digest is exactly the one a "[REDACTED]" prompt produces —
    # i.e. we committed to the redacted view, not to something merely different
    control_dir = tmp_path / "c"
    control_dir.mkdir()
    control = _listener(control_dir)
    _drive([control], prompt="[REDACTED]")
    assert b == _compute(_ledger(control_dir)[0])["agent_input_digest"]


def test_a_partial_redaction_does_not_leak_through_a_second_prompt_key(tmp_path):
    """The hazard this adapter's one-prompt-field rule exists for.

    litellm's ``model_call_details`` carries the same prompt under ``messages``
    AND ``input``. litellm's own ``perform_redaction`` clears both, but a custom
    ``async_logging_hook`` that rewrites only ``messages`` — the obvious thing to
    write — leaves ``input`` holding the original text. If this adapter sealed
    both, the un-redacted copy would decide the digest and the operator's
    redaction would be silently undone."""
    from litellm.integrations.custom_logger import CustomLogger

    secret = "my SSN is 123-45-6789"

    class MessagesOnlyRedactor(CustomLogger):
        async def async_logging_hook(self, kwargs, result, call_type):
            new_kwargs = dict(kwargs)
            new_kwargs["messages"] = [{"role": "user", "content": "[REDACTED]"}]
            assert new_kwargs.get("input"), "precondition: litellm also carries 'input'"
            return new_kwargs, result

    partial = _listener(tmp_path)
    _drive([MessagesOnlyRedactor(), partial], prompt=secret)

    control_dir = tmp_path / "ctl"
    control_dir.mkdir()
    _drive([_listener(control_dir)], prompt="[REDACTED]")

    leaked_dir = tmp_path / "leak"
    leaked_dir.mkdir()
    _drive([_listener(leaked_dir)], prompt=secret)

    partial_digest = _compute(_ledger(tmp_path)[0])["agent_input_digest"]
    assert partial_digest == _compute(_ledger(control_dir)[0])["agent_input_digest"]
    assert partial_digest != _compute(_ledger(leaked_dir)[0])["agent_input_digest"]


def test_exactly_one_prompt_field_is_sealed_and_it_is_named(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_kwargs(input=["also the prompt"]), RESPONSE)
    view = core._request_view(_kwargs(input=["also the prompt"]), withhold=False)
    assert view["prompt_field"] == "messages"
    assert "messages" not in view and "input" not in view


def test_embeddings_fall_back_to_the_input_field(tmp_path):
    kw = _kwargs(call_type="aembedding", input=["embed me"])
    kw.pop("messages")
    view = _core(tmp_path)._request_view(kw, withhold=False)
    assert view["prompt_field"] == "input"
    assert view["prompt"] == ["embed me"]


def test_the_caller_never_sees_our_listener(tmp_path):
    """Observation-only, end to end: the response the caller receives is
    byte-identical with and without the listener registered."""
    with_listener = _drive([_listener(tmp_path)], mock="the answer is 42")
    without = _drive([], mock="the answer is 42")
    assert with_listener.choices[0].message.content == "the answer is 42"
    assert without.choices[0].message.content == "the answer is 42"


def test_async_log_pre_api_call_is_a_dead_hook_in_this_release(tmp_path):
    """Pinned fact, not folklore: CustomLogger.async_log_pre_api_call is declared
    but never dispatched in the pinned release. If a future litellm starts
    calling it, this test fails and the adapter can seal a genuine PLANNED
    capsule instead of a post-hoc one. That is a good reason to fail."""
    from litellm.integrations.custom_logger import CustomLogger

    fired = []

    class Probe(CustomLogger):
        async def async_log_pre_api_call(self, model, messages, kwargs):
            fired.append("async")

        def log_pre_api_call(self, model, messages, kwargs):
            fired.append("sync")

    _drive([Probe()])
    assert "sync" in fired, "the sync pre-call hook should still fire"
    assert "async" not in fired, (
        "litellm now dispatches async_log_pre_api_call — revisit "
        "REQUEST_PROVENANCE and consider sealing a real planned capsule"
    )


# ---------------------------------------------------------------------------
# Client disconnect mid-stream: a success-shaped event over a partial response
# ---------------------------------------------------------------------------


def _disconnect_kwargs(where="metadata"):
    kw = _kwargs(stream=True)
    stamp = {"client_disconnected": True,
             "error_information": {"error_code": "499", "error_class": "ClientDisconnected"}}
    if where == "metadata":
        kw["metadata"] = stamp
    elif where == "litellm_metadata":
        kw["litellm_params"]["litellm_metadata"] = stamp
    else:
        kw["litellm_params"]["metadata"] = stamp
    return kw


def test_disconnected_stream_seals_dispatched_not_confirmed(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_disconnect_kwargs(), RESPONSE)
    outcome = _ledger(tmp_path)[-1]
    assert outcome["effect"]["status"] == "dispatched"
    assert outcome["disposition"]["verdict_class"] == "executed"
    comp = _compute(outcome)
    assert comp["client_disconnected"] is True
    assert comp["response_completeness"] == "partial"
    assert comp["litellm_call_id"] == "call-abc-123"


def test_disconnect_flag_under_litellm_params_metadata_is_honored(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_disconnect_kwargs(where="litellm_params"), RESPONSE)
    outcome = _ledger(tmp_path)[-1]
    assert outcome["effect"]["status"] == "dispatched"
    assert _compute(outcome)["response_completeness"] == "partial"


def test_completed_stream_still_seals_confirmed_without_partial_markers(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_kwargs(stream=True, metadata={"client_disconnected": False}), RESPONSE)
    outcome = _ledger(tmp_path)[-1]
    assert outcome["effect"]["status"] == "confirmed"
    comp = _compute(outcome)
    assert "client_disconnected" not in comp
    assert "response_completeness" not in comp


def test_disconnected_outcome_verifies_and_chains(tmp_path):
    from capsule_emit.verification import verify_capsule
    core = _core(tmp_path)
    core.on_success_core(_disconnect_kwargs(), RESPONSE)
    planned, outcome = _ledger(tmp_path)[-2:]
    assert verify_capsule(outcome).ok
    assert outcome["chain"]["parent_capsule_id"] == planned["capsule_id"]


def test_disconnect_flag_under_litellm_metadata_is_honored(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_disconnect_kwargs(where="litellm_metadata"), RESPONSE)
    assert _ledger(tmp_path)[-1]["effect"]["status"] == "dispatched"


def test_disconnect_flag_must_be_literally_true(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_kwargs(metadata={"client_disconnected": "yes"}), RESPONSE)
    assert _ledger(tmp_path)[-1]["effect"]["status"] == "confirmed"
