# SPDX-License-Identifier: Apache-2.0
"""capsule-emit#128 — a float tool argument must chain, not silently orphan.

Reported behavior (PyPI 0.5.1, desk repro 2026-08-31): a tool declared
``set_threshold(name: str, value: float)`` invoked with ``{"name": "risk",
"value": 0.75}`` under ``LangChainCapsuleListener`` produced ONE ledger record
— confirmed, ``chain: null`` — plus a RuntimeWarning, while the tool itself
ran normally. The planned capsule was dropped at the digest layer, so the
outcome record had nothing to chain to, and the ledger showed a tool outcome
with no committed plan behind it and no indication anything was missing.

The §5.1 rule is not loosened here. Raw floats remain forbidden in
digest-bearing fields; the adapters now do the deterministic serialization the
rule requires (RFC 8785 §3.2.2.3 decimal strings) before digesting, at the one
shared commitment path every adapter goes through.

Both listeners are covered because both feed that same path, and Agno's float
behavior had never been established. Agno gets sync **and** async_hook
coverage: it dispatches ``arun``/``aexecute`` through a separate hook.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import warnings

import pytest

from capsule_emit.adapters.agno_listener import AgnoCapsuleListener, AgnoListenerCore
from capsule_emit.adapters.langchain_listener import LangChainListenerCore
from capsule_emit.signing import verify_capsule_signature
from capsule_emit.verification import verify_capsule as verify

# The issue's exact tool signature and arguments.
SER = {"name": "set_threshold"}
FLOAT_ARGS = {"name": "risk", "value": 0.75}
RESULT = "threshold risk set to 0.75"


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _compute(cap):
    return cap.get("model_attestation", {}).get("compute_attestation", {})


def _lc(tmp_path, **kw):
    return LangChainListenerCore(
        operator="acme-co", developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl", anchor=False, **kw,
    )


def _agno_core(tmp_path, **kw):
    return AgnoListenerCore(
        operator="acme-co", developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl", anchor=False, **kw,
    )


def _agno_listener(tmp_path, **kw):
    return AgnoCapsuleListener(
        operator="acme-co", developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl", anchor=False, **kw,
    )


def _tool(**kwargs):
    return RESULT


async def _tool_async(**kwargs):
    return RESULT


def assert_chained_pair(caps, *, tool_name="set_threshold"):
    """The whole point of #128: two records, chained, both verifiable.

    Verification runs with ``store=`` so the chain link is actually resolved
    against the parent rather than deferred as a store-level check.
    """
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    planned, confirmed = caps
    assert tool_name in planned["action_id"]
    assert tool_name in confirmed["action_id"]
    assert confirmed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert confirmed["chain"]["relation"] == "confirms"
    for cap in caps:
        result = verify(cap, store=caps)
        assert result.ok, f"verify_capsule failed: {[f.code for f in result.findings]}"
        assert verify_capsule_signature(cap), "producer envelope did not verify"
        # no unresolved chain complaint once the parent is in the store
        assert "chain_parent_missing" not in [f.code for f in result.findings]
    # the marker is only for orphans; a healthy pair must not carry it
    assert "unchained_reason" not in _compute(confirmed)


# ---------------------------------------------------------------------------
# (1) The repro: LangChain
# ---------------------------------------------------------------------------


def test_langchain_float_arg_yields_chained_pair(tmp_path):
    core = _lc(tmp_path)
    rid = uuid.uuid4()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_start_core(SER, FLOAT_ARGS, rid)
        core.on_tool_end_core(RESULT, rid)
    assert [str(w.message) for w in caught] == [], "no capsule should be dropped now"
    assert_chained_pair(_ledger(tmp_path))


def test_langchain_float_arg_error_path_also_chains(tmp_path):
    core = _lc(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, FLOAT_ARGS, rid)
    planned_id = core.last.capsule_id
    core.on_tool_error_core(RuntimeError("boom"), rid)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["chain"]["parent_capsule_id"] == planned_id
    assert verify(caps[1], store=caps).ok


def test_langchain_float_output_also_seals(tmp_path):
    """The float can be on the way back out, too."""
    core = _lc(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, {"name": "risk"}, rid)
    core.on_tool_end_core({"threshold": 0.75}, rid)
    assert_chained_pair(_ledger(tmp_path))


# ---------------------------------------------------------------------------
# (2) The same, established for Agno — sync hook and async_hook
# ---------------------------------------------------------------------------


def test_agno_float_arg_yields_chained_pair(tmp_path):
    core = _agno_core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert core.wrap_call("set_threshold", _tool, FLOAT_ARGS) == RESULT
    assert [str(w.message) for w in caught] == []
    assert_chained_pair(_ledger(tmp_path))


def test_agno_async_float_arg_yields_chained_pair(tmp_path):
    core = _agno_core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = asyncio.run(core.wrap_call_async("set_threshold", _tool_async, FLOAT_ARGS))
    assert out == RESULT
    assert [str(w.message) for w in caught] == []
    assert_chained_pair(_ledger(tmp_path))


def test_agno_sync_hook_shell_float_arg_chains(tmp_path):
    """Through the actual ``listener.hook`` callable agno is handed."""
    listener = _agno_listener(tmp_path)
    out = listener.hook(
        function_name="set_threshold", function_call=_tool, arguments=FLOAT_ARGS
    )
    assert out == RESULT
    assert_chained_pair(_ledger(tmp_path))


def test_agno_async_hook_shell_float_arg_chains(tmp_path):
    """Through the actual ``listener.async_hook`` callable (arun/aexecute path)."""
    listener = _agno_listener(tmp_path)
    out = asyncio.run(
        listener.async_hook(
            function_name="set_threshold",
            function_call=_tool_async,
            arguments=FLOAT_ARGS,
        )
    )
    assert out == RESULT
    assert_chained_pair(_ledger(tmp_path))


def test_agno_float_arg_error_path_chains(tmp_path):
    core = _agno_core(tmp_path)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        core.wrap_call("set_threshold", _boom, FLOAT_ARGS)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert "unchained_reason" not in _compute(caps[1])


# ---------------------------------------------------------------------------
# Edge cases: nested / list floats, int vs float
# ---------------------------------------------------------------------------


NESTED_CASES = {
    "list_of_floats": {"name": "risk", "bands": [0.1, 0.5, 0.9]},
    "nested_dict_float": {"cfg": {"threshold": 0.75}},
    "float_in_list_of_dicts": {"rules": [{"lo": 0.1}, {"hi": 0.9}]},
    "deeply_nested": {"a": [{"b": [{"c": [0.5]}]}]},
    "mixed_int_float_str": {"n": 3, "v": 0.75, "s": "x", "b": True, "z": None},
    "negative_and_zero": {"a": -0.5, "b": -0.0, "c": 0.0},
    "exponent_range": {"small": 1e-7, "big": 1.5e21},
}


@pytest.mark.parametrize("label", sorted(NESTED_CASES))
def test_langchain_nested_and_list_floats_chain(tmp_path, label):
    core = _lc(tmp_path)
    rid = uuid.uuid4()
    core.on_tool_start_core(SER, NESTED_CASES[label], rid)
    core.on_tool_end_core(RESULT, rid)
    assert_chained_pair(_ledger(tmp_path))


@pytest.mark.parametrize("label", sorted(NESTED_CASES))
def test_agno_nested_and_list_floats_chain(tmp_path, label):
    core = _agno_core(tmp_path)
    core.wrap_call("set_threshold", _tool, NESTED_CASES[label])
    assert_chained_pair(_ledger(tmp_path))


@pytest.mark.parametrize("label", sorted(NESTED_CASES))
def test_agno_async_nested_and_list_floats_chain(tmp_path, label):
    core = _agno_core(tmp_path)
    asyncio.run(core.wrap_call_async("set_threshold", _tool_async, NESTED_CASES[label]))
    assert_chained_pair(_ledger(tmp_path))


def _input_digest(tmp_path, args, listener_factory):
    core = listener_factory(tmp_path)
    core.wrap_call("set_threshold", _tool, args)
    return _compute(_ledger(tmp_path)[0])["agent_input_digest"]


def test_int_one_and_float_one_seal_to_different_digests(tmp_path):
    """1 vs 1.0 — documented, deterministic divergence.

    ``1`` stays a JSON integer token (legal in a digest-bearing field, and
    preserving it is what keeps every previously sealed integer digest valid);
    ``1.0`` becomes the string ``"1"``, a float's only canonical carrier. So
    the two digest differently, and stably.
    """
    as_int = _input_digest(tmp_path / "i", {"v": 1}, _agno_core)
    as_float = _input_digest(tmp_path / "f", {"v": 1.0}, _agno_core)
    assert as_int != as_float

    # ...and each is reproducible on its own.
    assert as_int == _input_digest(tmp_path / "i2", {"v": 1}, _agno_core)
    assert as_float == _input_digest(tmp_path / "f2", {"v": 1.0}, _agno_core)


def test_float_one_and_string_one_seal_to_the_same_digest(tmp_path):
    """The corollary: ``1.0`` and the decimal string ``"1"`` are the same commitment.

    That is the intended equivalence — a producer that already followed §5.1 by
    hand-encoding decimal strings gets the identical digest as one that passes
    the float and lets the adapter canonicalize it.
    """
    as_float = _input_digest(tmp_path / "f", {"v": 1.0}, _agno_core)
    as_string = _input_digest(tmp_path / "s", {"v": "1"}, _agno_core)
    assert as_float == as_string


def test_float_arg_digest_is_reproducible_across_listeners(tmp_path):
    """One rule, one place: both listeners commit a float identically."""
    lc = _lc(tmp_path / "lc")
    lc.on_tool_start_core(SER, FLOAT_ARGS, uuid.uuid4())
    lc_digest = _compute(_ledger(tmp_path / "lc")[0])["agent_input_digest"]

    agno = _agno_core(tmp_path / "ag")
    agno.wrap_call("set_threshold", _tool, FLOAT_ARGS)
    agno_digest = _compute(_ledger(tmp_path / "ag")[0])["agent_input_digest"]

    assert lc_digest == agno_digest


# ---------------------------------------------------------------------------
# Edge cases: NaN / Infinity — loud, never silent
# ---------------------------------------------------------------------------

NON_REPRESENTABLE = [
    ("nan", float("nan")),
    ("posinf", float("inf")),
    ("neginf", float("-inf")),
]


@pytest.mark.parametrize("label, bad", NON_REPRESENTABLE, ids=[c[0] for c in NON_REPRESENTABLE])
def test_langchain_nan_inf_warns_and_marks_the_orphan(tmp_path, label, bad):
    """No JCS representation => warn-and-skip, but the orphan is self-describing.

    warn-and-skip (not raise) is the agent-safe contract: a listener must never
    turn a working tool call into a failed one. "Never silently" is satisfied
    by two things the pre-#128 code did not do — the warning names the exact
    field path, and the outcome record carries ``unchained_reason`` so the
    missing link is legible from the ledger alone.
    """
    core = _lc(tmp_path)
    rid = uuid.uuid4()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_start_core(SER, {"name": "risk", "value": bad}, rid)
        core.on_tool_end_core(RESULT, rid)

    messages = [str(w.message) for w in caught]
    assert any("agent_input.value" in m for m in messages), messages
    assert any(w.category is RuntimeWarning for w in caught)

    caps = _ledger(tmp_path)
    assert len(caps) == 1
    outcome = caps[0]
    assert outcome["effect"]["status"] == "confirmed"
    assert not outcome.get("chain")
    assert "unchained_reason" in _compute(outcome)
    # the real tool name survives — pre-#128 this degraded to "tool"
    assert "set_threshold" in outcome["action_id"]
    assert verify(outcome).ok


@pytest.mark.parametrize("label, bad", NON_REPRESENTABLE, ids=[c[0] for c in NON_REPRESENTABLE])
def test_agno_nan_inf_warns_marks_orphan_and_tool_still_runs(tmp_path, label, bad):
    core = _agno_core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # a raising hook would be reported by agno as the tool's own failure
        assert core.wrap_call("set_threshold", _tool, {"value": bad}) == RESULT

    messages = [str(w.message) for w in caught]
    assert any("agent_input.value" in m for m in messages), messages
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert not caps[0].get("chain")
    assert "unchained_reason" in _compute(caps[0])


@pytest.mark.parametrize("label, bad", NON_REPRESENTABLE, ids=[c[0] for c in NON_REPRESENTABLE])
def test_agno_async_nan_inf_warns_and_marks_orphan(tmp_path, label, bad):
    core = _agno_core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = asyncio.run(core.wrap_call_async("set_threshold", _tool_async, {"value": bad}))
    assert out == RESULT
    assert any("agent_input.value" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert "unchained_reason" in _compute(caps[0])


def test_nan_in_a_nested_field_names_the_path(tmp_path):
    core = _lc(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_tool_start_core(SER, {"cfg": {"bands": [1.0, float("nan")]}}, uuid.uuid4())
    assert any("agent_input.cfg.bands[1]" in str(w.message) for w in caught), [
        str(w.message) for w in caught
    ]


def test_agno_nan_failed_path_also_marks_the_orphan(tmp_path):
    core = _agno_core(tmp_path)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError):
            core.wrap_call("set_threshold", _boom, {"value": float("nan")})
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "failed"
    assert "unchained_reason" in _compute(caps[0])


def test_unmatched_end_is_not_reported_as_a_dropped_plan(tmp_path):
    """An end with no start never had a plan to lose — no false orphan marker."""
    core = _lc(tmp_path)
    core.on_tool_end_core(RESULT, uuid.uuid4())
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert not caps[0].get("chain")
    assert "unchained_reason" not in _compute(caps[0])


# ---------------------------------------------------------------------------
# Shell: a real langchain-core tool invocation, the desk's exact repro
# ---------------------------------------------------------------------------


def test_shell_real_langchain_float_tool_e2e(tmp_path):
    lc_tools = pytest.importorskip("langchain_core.tools")
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    @lc_tools.tool
    def set_threshold(name: str, value: float) -> str:
        """Set a named risk threshold."""
        return f"threshold {name} set to {value}"

    listener = LangChainCapsuleListener(
        operator="acme-co", developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl", anchor=False,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = set_threshold.invoke(
            {"name": "risk", "value": 0.75}, config={"callbacks": [listener]}
        )
    assert out == "threshold risk set to 0.75"
    assert [str(w.message) for w in caught if w.category is RuntimeWarning] == []
    assert_chained_pair(_ledger(tmp_path))
