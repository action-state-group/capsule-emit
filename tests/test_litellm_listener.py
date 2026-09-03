# SPDX-License-Identifier: Apache-2.0
"""LiteLLM proxy listener tests — framework-free core + real-wheel shell.

Sealing logic lives in ``LiteLLMListenerCore``, whose two entry points take plain
dicts, so the full behavior is exercised WITHOUT litellm installed (mirrors the
CrewAI/LangChain/agno listener test approach). The ``CustomLogger`` shell and the
config-load path are covered by importorskip'd tests at the bottom that drive the
REAL released litellm wheel.

Covered:
- planned → confirmed chain on success; planned → failed chain on failure
- observation_mode="post_hoc_event" on every capsule, and the request capsule's
  request_record_provenance (it is NOT a pre-execution commitment and says so)
- the failure path withholds the prompt by default and stamps the reason;
  opt-in restores it
- credential hygiene: litellm_params (api_key, azure_password, client_secret)
  never reaches the digest layer, on either path
- digest-only privacy: raw prompt/response text never lands in the ledger
- never-raises: a broken ledger warns and the handler still returns
- unchained_reason when the request capsule is disabled or could not be sealed
- litellm_call_id is stamped on both halves so a reader can join them
- payload bound: an oversized payload is replaced by a RECORDED truncation
  marker, never silently clipped
- floats in a payload canonicalize and still chain (capsule-emit#128)
- shell: async_post_call_failure_hook returns None (never transforms the client's
  error), async_pre_call_hook is NOT overridden (gate layer, not ours)
- config-load: the dotted string resolves through litellm's own get_instance_fn
  and initialize_callbacks_on_proxy; the CLASS is rejected, the INSTANCE accepted
- end to end against the real wheel: a hermetic litellm.acompletion with
  mock_response seals a verifying chain, and a sibling redaction callback's
  output is what we digest
"""
from __future__ import annotations

import asyncio
import json
import warnings

import pytest

from capsule_emit.adapters.litellm_listener import (
    OBSERVATION_MODE,
    REQUEST_PROVENANCE,
    WITHHELD_REASON,
    LiteLLMListenerCore,
)
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> LiteLLMListenerCore:
    return LiteLLMListenerCore(
        operator="acme-co",
        developer="gateway@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _compute(cap):
    return cap.get("model_attestation", {}).get("compute_attestation", {})


PROMPT = "my SSN is 123-45-6789"

#: A realistic ``model_call_details`` as litellm hands it to
#: ``async_log_success_event`` — including the credential-bearing
#: ``litellm_params`` that must never reach the digest layer.
def _kwargs(**over):
    base = {
        "model": "gpt-4o-mini",
        "call_type": "acompletion",
        "custom_llm_provider": "openai",
        "litellm_call_id": "call-abc-123",
        "messages": [{"role": "user", "content": PROMPT}],
        "user": "u1",
        "stream": False,
        "litellm_params": {
            "api_key": "sk-SUPERSECRET",
            "azure_password": "hunter2",
            "client_secret": "cs-SUPERSECRET",
            "litellm_call_id": "call-abc-123",
        },
        "optional_params": {"temperature": 0.7},
    }
    base.update(over)
    return base


RESPONSE = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "object": "chat.completion",
    "created": 1,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "the answer"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    "_hidden_params": {"api_key": "sk-LEAK"},
}


def _request_data(**over):
    base = {
        "model": "gpt-4o-mini",
        "call_type": "acompletion",
        "messages": [{"role": "user", "content": PROMPT}],
        "litellm_call_id": "call-fail-9",
        "litellm_params": {"api_key": "sk-SUPERSECRET"},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The two-record chain
# ---------------------------------------------------------------------------


def test_success_seals_planned_then_confirmed(tmp_path):
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert all(verify(c).ok for c in caps)


def test_confirmed_chains_to_planned(tmp_path):
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_failure_seals_planned_then_failed_chained(tmp_path):
    _core(tmp_path).on_failure_core(_request_data(), RuntimeError("upstream exploded"))
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_failure_records_the_exception_class(tmp_path):
    _core(tmp_path).on_failure_core(_request_data(), ValueError("bad model"))
    assert _compute(_ledger(tmp_path)[1])["exception_class"] == "ValueError"


def test_action_and_model_carry_the_call(tmp_path):
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert "litellm.acompletion" in caps[0]["action_id"]
    att = caps[0]["model_attestation"]
    assert att["model_id"] == "gpt-4o-mini"
    assert att["provider"] == "openai"


def test_provider_is_inferred_when_litellm_does_not_say(tmp_path):
    kw = _kwargs(model="claude-3-5-sonnet")
    kw.pop("custom_llm_provider")
    _core(tmp_path).on_success_core(kw, RESPONSE)
    assert _ledger(tmp_path)[0]["model_attestation"]["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# Provenance — the request record is post-hoc and says so
# ---------------------------------------------------------------------------


def test_every_capsule_stamps_observation_mode(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(_kwargs(), RESPONSE)
    core.on_failure_core(_request_data(), RuntimeError("boom"))
    caps = _ledger(tmp_path)
    assert len(caps) == 4
    assert all(_compute(c)["observation_mode"] == OBSERVATION_MODE for c in caps)


def test_request_capsule_declares_it_was_sealed_after_the_fact(tmp_path):
    """The whole honesty of this adapter rests on this stamp. ``planned`` is the
    profile's "asserts no execution" carve and carries no timing claim; the
    timing claim lives here, and a reader must not have to infer it."""
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert _compute(caps[0])["request_record_provenance"] == REQUEST_PROVENANCE
    assert "not witnessed before execution" in _compute(caps[0])["request_record_provenance"]
    # the outcome half carries no provenance stamp — it observed what it says
    assert "request_record_provenance" not in _compute(caps[1])


def test_planned_carve_keeps_the_request_capsule_verifiable(tmp_path):
    """Regression guard for the build-shift mistake this replaced: effect.status
    is a CLOSED vocabulary (agent_action_capsule.contracts.derive_effect_mode).
    An invented value falls through to 'dispatched_unconfirmed', which §5.2 then
    requires an effect_attestation for — so the capsule stops verifying."""
    from agent_action_capsule.contracts import derive_effect_mode

    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    request_effect = _ledger(tmp_path)[0]["effect"]
    assert derive_effect_mode(request_effect) == "not_applicable"
    assert derive_effect_mode({"type": "llm_call", "status": "requested"}) == (
        "dispatched_unconfirmed"
    )


def test_call_id_is_stamped_on_both_halves(tmp_path):
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert all(_compute(c)["litellm_call_id"] == "call-abc-123" for c in caps)


def test_call_id_falls_back_to_litellm_params(tmp_path):
    kw = _kwargs()
    kw.pop("litellm_call_id")
    _core(tmp_path).on_success_core(kw, RESPONSE)
    assert _compute(_ledger(tmp_path)[0])["litellm_call_id"] == "call-abc-123"


# ---------------------------------------------------------------------------
# The failure path withholds the un-redacted prompt by default
# ---------------------------------------------------------------------------


def test_failure_withholds_the_prompt_by_default_and_says_why(tmp_path):
    _core(tmp_path).on_failure_core(_request_data(), RuntimeError("boom"))
    req = _ledger(tmp_path)[0]
    assert _compute(req)["request_payload_withheld"] is True
    assert _compute(req)["withheld_reason"] == WITHHELD_REASON


def test_failure_payload_can_be_opted_into(tmp_path):
    core = _core(tmp_path, include_unredacted_failure_payload=True)
    core.on_failure_core(_request_data(), RuntimeError("boom"))
    req = _ledger(tmp_path)[0]
    assert "request_payload_withheld" not in _compute(req)
    assert "withheld_reason" not in _compute(req)


def test_withholding_changes_the_digest(tmp_path):
    """Not cosmetic: the withheld and included forms commit to different
    preimages, so a verifier cannot mistake one for the other."""
    a = _core(tmp_path / "a")
    (tmp_path / "a").mkdir()
    a.on_failure_core(_request_data(), RuntimeError("boom"))
    b = _core(tmp_path / "b", include_unredacted_failure_payload=True)
    (tmp_path / "b").mkdir()
    b.on_failure_core(_request_data(), RuntimeError("boom"))
    da = _compute(_ledger(tmp_path / "a")[0])["agent_input_digest"]
    db = _compute(_ledger(tmp_path / "b")[0])["agent_input_digest"]
    assert da != db


# ---------------------------------------------------------------------------
# Credential hygiene and digest-only privacy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", ["sk-SUPERSECRET", "hunter2", "cs-SUPERSECRET", "sk-LEAK"])
def test_no_credential_reaches_the_ledger(tmp_path, secret):
    core = _core(tmp_path, include_unredacted_failure_payload=True)
    core.on_success_core(_kwargs(), RESPONSE)
    core.on_failure_core(_request_data(), RuntimeError("boom"))
    assert secret not in (tmp_path / "ledger.jsonl").read_text()


def test_litellm_params_is_not_in_the_allowlist():
    from capsule_emit.adapters.litellm_listener import (
        _CREDENTIAL_MARKERS,
        _PROMPT_FIELDS,
        _REQUEST_FIELDS,
        _RESPONSE_FIELDS,
    )

    every = set(_REQUEST_FIELDS) | set(_PROMPT_FIELDS) | set(_RESPONSE_FIELDS)
    assert "litellm_params" not in every
    assert "optional_params" not in every
    for field in every:
        for marker in _CREDENTIAL_MARKERS:
            assert marker not in field, f"{field!r} looks credential-shaped"


def test_prompt_and_response_text_stay_out_of_the_ledger(tmp_path):
    _core(tmp_path).on_success_core(_kwargs(), RESPONSE)
    text = (tmp_path / "ledger.jsonl").read_text()
    assert PROMPT not in text
    assert "the answer" not in text
    # but the commitment is there
    assert _compute(_ledger(tmp_path)[0])["agent_input_digest"]
    assert _compute(_ledger(tmp_path)[1])["agent_output_digest"]


def test_response_is_reduced_to_the_allowlist(tmp_path):
    """_hidden_params (which carries api_key) is dropped, not digested."""
    core = _core(tmp_path)
    core.on_success_core(_kwargs(), RESPONSE)
    clean = {k: v for k, v in RESPONSE.items() if k != "_hidden_params"}
    other = _core(tmp_path / "o")
    (tmp_path / "o").mkdir()
    other.on_success_core(_kwargs(), clean)
    assert (
        _compute(_ledger(tmp_path)[1])["agent_output_digest"]
        == _compute(_ledger(tmp_path / "o")[1])["agent_output_digest"]
    )


# ---------------------------------------------------------------------------
# Never-raises, and the honest un-chained record
# ---------------------------------------------------------------------------


def test_broken_ledger_warns_and_does_not_raise(tmp_path):
    # a ledger path whose parent is an existing FILE cannot be opened or created
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    core = LiteLLMListenerCore(
        operator="acme-co",
        developer="gateway@v1",
        ledger=blocker / "ledger.jsonl",
        anchor=False,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_success_core(_kwargs(), RESPONSE)
        core.on_failure_core(_request_data(), RuntimeError("boom"))
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_disabled_request_record_leaves_an_honest_unchained_stamp(tmp_path):
    _core(tmp_path, include_request_record=False).on_success_core(_kwargs(), RESPONSE)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert (caps[0].get("chain") or {}).get("parent_capsule_id") is None
    assert "include_request_record=False" in _compute(caps[0])["unchained_reason"]


def test_non_dict_input_does_not_raise(tmp_path):
    core = _core(tmp_path)
    core.on_success_core(None, RESPONSE)  # type: ignore[arg-type]
    core.on_failure_core("not a dict", None)  # type: ignore[arg-type]
    assert len(_ledger(tmp_path)) == 4


# ---------------------------------------------------------------------------
# Payload bound — truncation is recorded, never silent
# ---------------------------------------------------------------------------


def test_oversized_payload_is_replaced_by_a_recorded_marker(tmp_path):
    huge = _kwargs(messages=[{"role": "user", "content": "x" * 50_000}])
    bounded = _core(tmp_path, max_payload_chars=500)
    bounded.on_success_core(huge, RESPONSE)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert all(verify(c).ok for c in caps)

    view = bounded._request_view(huge, withhold=False)
    assert view["prompt"]["capsule_emit_truncated"] is True
    assert view["prompt"]["original_repr_chars"] > 50_000
    # the identifying scalars survive the bound — an oversized call still says
    # which model it was
    assert view["model"] == "gpt-4o-mini"
    assert view["call_type"] == "acompletion"

    # a truncated preimage must not collide with the faithful one
    faithful = _core(tmp_path / "s", max_payload_chars=0)
    (tmp_path / "s").mkdir()
    faithful.on_success_core(huge, RESPONSE)
    assert (
        _compute(caps[0])["agent_input_digest"]
        != _compute(_ledger(tmp_path / "s")[0])["agent_input_digest"]
    )


def test_zero_disables_the_bound(tmp_path):
    huge = _kwargs(messages=[{"role": "user", "content": "x" * 50_000}])
    a = _core(tmp_path, max_payload_chars=0)
    a.on_success_core(huge, RESPONSE)
    b = _core(tmp_path / "b", max_payload_chars=10_000_000)
    (tmp_path / "b").mkdir()
    b.on_success_core(huge, RESPONSE)
    assert (
        _compute(_ledger(tmp_path)[0])["agent_input_digest"]
        == _compute(_ledger(tmp_path / "b")[0])["agent_input_digest"]
    )


# ---------------------------------------------------------------------------
# Floats (capsule-emit#128 / #135)
# ---------------------------------------------------------------------------


def test_float_payload_canonicalizes_and_still_chains(tmp_path):
    resp = dict(RESPONSE, usage={"total_tokens": 30, "cost": 0.5})
    _core(tmp_path).on_success_core(_kwargs(), resp)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_uncanonicalizable_float_warns_and_does_not_crash(tmp_path):
    resp = dict(RESPONSE, usage={"cost": float("nan")})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _core(tmp_path).on_success_core(_kwargs(), resp)
    caps = _ledger(tmp_path)
    # the request half still seals; the response half degrades loudly
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "planned"
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


# ---------------------------------------------------------------------------
# listener_from_env — the config.yaml load path passes no constructor args
# ---------------------------------------------------------------------------


def test_from_env_refuses_to_guess_the_operator(monkeypatch):
    from capsule_emit.adapters import litellm_listener as mod

    monkeypatch.delenv("CAPSULE_EMIT_OPERATOR", raising=False)
    monkeypatch.delenv("CAPSULE_EMIT_DEVELOPER", raising=False)
    with pytest.raises(ValueError) as exc:
        mod.listener_from_env()
    assert "CAPSULE_EMIT_OPERATOR" in str(exc.value)
    assert "CAPSULE_EMIT_DEVELOPER" in str(exc.value)


def test_importing_the_module_has_no_side_effects(monkeypatch):
    """``proxy_handler_instance`` is built on getattr (PEP 562), not on import,
    so importing under test with no environment set must stay clean."""
    import importlib

    monkeypatch.delenv("CAPSULE_EMIT_OPERATOR", raising=False)
    mod = importlib.import_module("capsule_emit.adapters.litellm_listener")
    importlib.reload(mod)
    with pytest.raises(ValueError):
        _ = mod.proxy_handler_instance


def test_unknown_module_attribute_still_raises_attribute_error():
    from capsule_emit.adapters import litellm_listener as mod

    with pytest.raises(AttributeError):
        _ = mod.no_such_thing


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
