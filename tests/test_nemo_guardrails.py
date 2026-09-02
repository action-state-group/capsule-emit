# SPDX-License-Identifier: Apache-2.0
"""Tests for the NeMo Guardrails rail-decision recorder.

Mirrors tests/test_strands_listener.py: the framework-free core is exercised with
duck-typed stand-ins (no nemoguardrails import), then a smaller set of live-engine
tests runs the real ``LLMRails`` when the SDK is installed.

Contract lines cited in assertions were read from the released
``nemoguardrails==0.24.0`` wheel.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import warnings
from dataclasses import dataclass, field
from typing import Any

import pytest

from capsule_emit.adapters.nemo_guardrails import (
    ACTION_PARAM_NAME,
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_TRANSFORM,
    DECISION_UNKNOWN,
    LOG_ADAPTER_NAME,
    RAIL_ACTION_NAME,
    TURN_ACTION,
    NeMoGuardrailsCapsuleRecorder,
    NeMoRailsCore,
    _scrub,
    get_capsule_log_adapter_class,
    register_capsule_log_adapter,
)

try:  # pragma: no cover - environment dependent
    import nemoguardrails as _ng  # noqa: F401

    HAS_NG = True
except Exception:  # pragma: no cover
    HAS_NG = False

requires_ng = pytest.mark.skipif(not HAS_NG, reason="nemoguardrails not installed")


# --------------------------------------------------------------------------
# Duck-typed stand-ins for the engine's log objects. Field names match
# nemoguardrails/rails/llm/options.py:219 (ExecutedAction) and :234 (ActivatedRail),
# and actions/rail_outcome.py:66/:80 (TransformSpec / RailOutcome).
# --------------------------------------------------------------------------
@dataclass
class FakeTransform:
    target: str
    text: str


@dataclass
class FakeOutcome:
    decision: str
    reason: str | None = None
    metadata: dict = field(default_factory=dict)
    transforms: tuple = ()
    failed: bool = False


@dataclass
class FakeAction:
    action_name: str = "check"
    action_params: dict = field(default_factory=dict)
    return_value: Any = None
    duration: float | None = None


@dataclass
class FakeRail:
    type: str = "input"
    name: str = "self check input"
    decisions: list = field(default_factory=list)
    executed_actions: list = field(default_factory=list)
    stop: bool = False
    duration: float | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class FakeInteractionLog:
    id: str = "interaction-1"
    activated_rails: list = field(default_factory=list)


def allow_rail(**kw):
    kw.setdefault("executed_actions", [FakeAction(return_value=FakeOutcome(DECISION_ALLOW))])
    return FakeRail(**kw)


def block_rail(**kw):
    kw.setdefault("executed_actions", [FakeAction(return_value=FakeOutcome(DECISION_BLOCK))])
    kw.setdefault("stop", True)
    return FakeRail(**kw)


def transform_rail(**kw):
    outcome = FakeOutcome(
        DECISION_TRANSFORM, transforms=(FakeTransform("user_message", "[REDACTED] hello"),)
    )
    kw.setdefault("executed_actions", [FakeAction(return_value=outcome)])
    return FakeRail(**kw)



@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """asyncio.run() (used by the async-path tests) leaves the main thread's loop
    slot cleared; nemoguardrails' sync entry points call get_or_create_event_loop(),
    which then has to build one mid-test. Install a fresh loop per test — same
    guard tests/test_agno_listener.py uses."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    asyncio.set_event_loop(None)
    loop.close()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_ANCHOR", "off")
    return tmp_path / "ledger.jsonl"


def rows(ledger_path):
    if not os.path.exists(ledger_path):
        return []
    with open(ledger_path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def core(ledger_path, **kw):
    kw.setdefault("operator", "acme-co")
    kw.setdefault("developer", "support-bot@v1")
    return NeMoRailsCore(ledger=ledger_path, **kw)


def compute(row):
    return row["model_attestation"]["compute_attestation"]


def verdict(row):
    return row["disposition"]["verdict_class"]


def status(row):
    return row["effect"]["status"]


# --------------------------------------------------------------------------
# classify(): the RailDecision spine
# --------------------------------------------------------------------------
def test_classify_reads_the_typed_rail_outcome_allow():
    assert NeMoRailsCore.classify(allow_rail()) == (DECISION_ALLOW, False)


def test_classify_reads_the_typed_rail_outcome_block():
    assert NeMoRailsCore.classify(block_rail()) == (DECISION_BLOCK, False)


def test_classify_reads_the_typed_rail_outcome_transform():
    assert NeMoRailsCore.classify(transform_rail()) == (DECISION_TRANSFORM, False)


def test_classify_reports_a_failed_rail_separately_from_a_deliberate_block():
    """RailOutcome.failure() is a BLOCK with failed=True (rail_outcome.py:148-160)."""
    rail = FakeRail(executed_actions=[FakeAction(return_value=FakeOutcome(DECISION_BLOCK, failed=True))])
    assert NeMoRailsCore.classify(rail) == (DECISION_BLOCK, True)


def test_classify_accepts_an_enum_member_whose_value_is_the_string():
    """RailDecision is a `str, Enum`; .value and the member both compare equal."""

    class Decision(str):
        value = DECISION_BLOCK

    rail = FakeRail(executed_actions=[FakeAction(return_value=FakeOutcome(Decision(DECISION_BLOCK)))])
    assert NeMoRailsCore.classify(rail)[0] == DECISION_BLOCK


def test_classify_falls_back_to_stop_when_no_action_returned_an_outcome():
    """Older community rails and dialog rails expose no RailOutcome."""
    assert NeMoRailsCore.classify(FakeRail(stop=True, executed_actions=[])) == (DECISION_BLOCK, False)
    assert NeMoRailsCore.classify(FakeRail(stop=False, executed_actions=[])) == (DECISION_ALLOW, False)


def test_classify_says_unknown_rather_than_guessing_a_pass():
    """Absent is never pass: an unclassifiable rail is 'unknown', not 'allow'."""
    rail = FakeRail(stop=None, executed_actions=[FakeAction(return_value="some legacy string")])
    assert NeMoRailsCore.classify(rail) == (DECISION_UNKNOWN, False)


def test_classify_ignores_actions_that_returned_no_outcome_and_finds_the_one_that_did():
    rail = FakeRail(
        executed_actions=[
            FakeAction(action_name="retrieve_relevant_chunks", return_value="\n"),
            FakeAction(action_name="self_check_input", return_value=FakeOutcome(DECISION_BLOCK)),
        ]
    )
    assert NeMoRailsCore.classify(rail) == (DECISION_BLOCK, False)


# --------------------------------------------------------------------------
# decision -> capsule vocabulary
# --------------------------------------------------------------------------
def test_allow_seals_executed_confirmed(ledger):
    core(ledger).record_rail(allow_rail())
    (row,) = rows(ledger)
    assert verdict(row) == "executed"
    assert status(row) == "confirmed"
    assert compute(row)["nemo_rail_decision"] == DECISION_ALLOW


def test_block_seals_blocked_and_keeps_effect_planned(ledger):
    """planned, not an ad-hoc status: an unreserved status would derive
    effect_mode=dispatched_unconfirmed and claim the content passed."""
    core(ledger).record_rail(block_rail())
    (row,) = rows(ledger)
    assert verdict(row) == "blocked"
    assert status(row) == "planned"
    assert row["assurance"]["effect_mode"] != "dispatched_unconfirmed"


def test_failed_rail_is_errored_not_blocked(ledger):
    """A rail that crashed and a rail that refused are different facts."""
    rail = FakeRail(executed_actions=[FakeAction(return_value=FakeOutcome(DECISION_BLOCK, failed=True))])
    core(ledger).record_rail(rail)
    (row,) = rows(ledger)
    assert verdict(row) == "errored"
    assert status(row) == "failed"
    assert compute(row)["nemo_rail_failed"] is True


def test_transform_records_targets_without_minting_a_new_verdict(ledger):
    """Vocabulary decision: no 'altered' verdict_class is invented adapter-locally."""
    core(ledger).record_rail(transform_rail())
    (row,) = rows(ledger)
    assert verdict(row) == "executed"
    assert compute(row)["nemo_rail_decision"] == DECISION_TRANSFORM
    assert compute(row)["nemo_transform_targets"] == ["user_message"]


def test_unknown_decision_does_not_claim_a_confirmed_effect(ledger):
    rail = FakeRail(stop=None, executed_actions=[FakeAction(return_value="legacy")])
    core(ledger).record_rail(rail)
    (row,) = rows(ledger)
    assert status(row) == "planned"
    assert compute(row)["nemo_rail_decision"] == DECISION_UNKNOWN


# --------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------
def test_record_turn_chains_head_rails_and_tail(ledger):
    results = core(ledger).record_turn(
        [allow_rail(type="input"), block_rail(type="output", name="self check output")],
        interaction_id="i-1",
        turn_input=[{"role": "user", "content": "hi"}],
        turn_output="refused",
    )
    assert len(results) == 4  # head + 2 rails + tail
    recorded = rows(ledger)
    assert [r["action_id"].split("/")[0] for r in recorded] == [
        TURN_ACTION,
        "rail:input:self check input",
        "rail:output:self check output",
        TURN_ACTION,
    ]
    ids = [r["capsule_id"] for r in recorded]
    parents = [r.get("chain", {}).get("parent_capsule_id") for r in recorded]
    assert parents[0] is None
    assert parents[1:] == ids[:-1], "each link confirms the previous capsule"
    assert all(r["chain"]["relation"] == "confirms" for r in recorded[1:])


def test_turn_tail_carries_the_aggregate_verdict(ledger):
    core(ledger).record_turn([allow_rail(), block_rail(type="output")])
    tail = rows(ledger)[-1]
    assert verdict(tail) == "blocked"
    assert compute(tail)["nemo_turn_phase"] == "close"
    assert compute(tail)["nemo_rail_decisions"] == [
        "input:self check input=allow",
        "output:self check input=block",
    ]


def test_turn_tail_is_executed_when_every_rail_allowed(ledger):
    core(ledger).record_turn([allow_rail(), allow_rail(type="output")])
    tail = rows(ledger)[-1]
    assert verdict(tail) == "executed"
    assert status(tail) == "confirmed"


def test_turn_tail_prefers_errored_over_blocked_when_a_rail_crashed(ledger):
    crashed = FakeRail(executed_actions=[FakeAction(return_value=FakeOutcome(DECISION_BLOCK, failed=True))])
    core(ledger).record_turn([block_rail(), crashed])
    tail = rows(ledger)[-1]
    assert verdict(tail) == "errored"


def test_turn_envelope_can_be_switched_off(ledger):
    core(ledger, include_turn_envelope=False).record_turn([allow_rail(), block_rail(type="output")])
    recorded = rows(ledger)
    assert len(recorded) == 2
    assert all(not r["action_id"].startswith(TURN_ACTION) for r in recorded)
    assert recorded[1]["chain"]["parent_capsule_id"] == recorded[0]["capsule_id"]


def test_empty_turn_still_seals_the_envelope(ledger):
    """A turn where no rails fired is a fact worth recording, not silence."""
    core(ledger).record_turn([], interaction_id="i-2")
    recorded = rows(ledger)
    assert len(recorded) == 2
    assert compute(recorded[0])["nemo_activated_rail_count"] == 0


def test_a_filtered_rail_does_not_break_the_chain(ledger):
    """record_allow=False drops allow capsules; the chain must still be continuous."""
    core(ledger, record_allow=False).record_turn([allow_rail(), block_rail(type="output")])
    recorded = rows(ledger)
    assert len(recorded) == 3  # head + block + tail
    ids = [r["capsule_id"] for r in recorded]
    assert [r["chain"].get("parent_capsule_id") for r in recorded[1:]] == ids[:-1]


# --------------------------------------------------------------------------
# floats — the trap that would silently seal nothing
# --------------------------------------------------------------------------
def test_rail_timings_are_float_scrubbed_not_dropped(ledger):
    """ActivatedRail.duration/started_at/finished_at are raw floats (options.py:234).

    Unprojected, they raise FloatInDigestError and every capsule fails closed —
    which looks exactly like 'no rails ran'. The timings must survive as strings.
    """
    rail = block_rail(duration=0.375, started_at=1756745000.5, finished_at=1756745000.875)
    rail.executed_actions[0].duration = 0.125
    results = core(ledger).record_rail(rail)
    assert results is not None, "a float-bearing rail must still seal"
    (row,) = rows(ledger)
    assert verdict(row) == "blocked"


def test_scrub_converts_floats_to_canonical_strings():
    assert _scrub(0.5) == "0.5"
    assert _scrub({"d": 1.25, "n": 3, "s": "x"}) == {"d": "1.25", "n": 3, "s": "x"}
    assert _scrub([1.5, [2.5]]) == ["1.5", ["2.5"]]


def test_scrub_keeps_bools_as_bools_not_ints():
    """bool is an int subclass; a bool that became 1 would corrupt the record."""
    out = _scrub({"stop": True, "count": 1})
    assert out["stop"] is True
    assert out["count"] == 1


def test_scrub_is_depth_bounded_against_a_self_referential_metadata_dict():
    d: dict = {}
    d["self"] = d
    assert "<omitted:depth>" in json.dumps(_scrub(d))


def test_scrub_omits_binary_payloads_by_length():
    assert _scrub(b"1234") == "<omitted:4 bytes>"


def test_scrub_handles_nonfinite_floats_without_raising():
    assert _scrub(float("nan")) == "<omitted:nonfinite>"
    assert _scrub(float("inf")) == "<omitted:nonfinite>"


def test_scrub_unpacks_pydantic_style_objects_via_model_dump():
    class Model:
        def model_dump(self, mode=None):
            return {"duration": 0.25}

    assert _scrub(Model()) == {"duration": "0.25"}


def test_scrub_falls_back_to_slots_for_frozen_dataclasses():
    """RailOutcome/TransformSpec are slots=True dataclasses with no __dict__."""

    class Slotted:
        __slots__ = ("text", "score")

        def __init__(self):
            self.text = "hi"
            self.score = 0.5

    assert _scrub(Slotted()) == {"text": "hi", "score": "0.5"}


# --------------------------------------------------------------------------
# digest-only
# --------------------------------------------------------------------------
def test_raw_user_message_never_reaches_the_ledger(ledger):
    """ExecutedAction.action_params routinely holds the raw user message."""
    secret = "my social security number is 123-45-6789"
    rail = block_rail(
        executed_actions=[
            FakeAction(
                action_name="self_check_input",
                action_params={"user_input": secret},
                return_value=FakeOutcome(DECISION_BLOCK, reason=secret),
            )
        ]
    )
    core(ledger).record_rail(rail)
    blob = open(ledger).read()
    assert secret not in blob
    assert "123-45-6789" not in blob
    assert compute(rows(ledger)[0])["agent_input_digest"]


def test_transform_text_is_committed_by_digest_not_stored(ledger):
    core(ledger).record_rail(transform_rail())
    blob = open(ledger).read()
    assert "[REDACTED] hello" not in blob
    assert compute(rows(ledger)[0])["agent_input_digest"]


def test_two_different_rail_bodies_produce_different_input_digests(ledger):
    c = core(ledger)
    c.record_rail(block_rail(name="rail-a"))
    c.record_rail(block_rail(name="rail-b"))
    a, b = (compute(r)["agent_input_digest"] for r in rows(ledger))
    assert a != b


# --------------------------------------------------------------------------
# never-raises
# --------------------------------------------------------------------------
def test_a_broken_ledger_warns_and_never_raises(tmp_path, monkeypatch):
    """Load-bearing: on the tracing path an exception propagates out of
    the caller's generate_async (llmrails.py:1314 is not wrapped in a try)."""
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    # a path whose parent component is a regular file can never be created
    c = NeMoRailsCore(operator="o", developer="d", ledger=str(blocker / "ledger.jsonl"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert c.record_rail(block_rail()) is None
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_record_turn_survives_a_rail_object_that_raises_on_attribute_access(ledger):
    class Hostile:
        type = "input"
        name = "hostile"

        @property
        def executed_actions(self):
            raise RuntimeError("boom")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = core(ledger).record_turn([Hostile(), block_rail(type="output")])
    # The hostile rail is skipped loudly; the healthy one and the envelope still seal.
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    actions = [r["action_id"].split("/")[0] for r in rows(ledger)]
    assert "rail:output:self check input" in actions
    assert results
    ids = [r["capsule_id"] for r in rows(ledger)]
    parents = [r.get("chain", {}).get("parent_capsule_id") for r in rows(ledger)]
    assert parents[1:] == ids[:-1], "a skipped rail must not orphan the chain"


def test_log_adapter_transform_swallows_recorder_failure():
    cls = get_capsule_log_adapter_class() if HAS_NG else None
    if cls is None:
        pytest.skip("nemoguardrails not installed")
    adapter = cls(operator="o", developer="d")

    class Boom:
        @property
        def activated_rails(self):
            raise RuntimeError("boom")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter.transform(Boom())
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


# --------------------------------------------------------------------------
# filtering / config
# --------------------------------------------------------------------------
def test_rail_types_filter_excludes_unselected_types(ledger):
    c = core(ledger, rail_types=("input",), include_turn_envelope=False)
    c.record_turn([allow_rail(type="input"), allow_rail(type="dialog")])
    assert len(rows(ledger)) == 1
    assert rows(ledger)[0]["action_id"].startswith("rail:input:")


def test_record_allow_false_still_records_a_failed_allow_shaped_rail(ledger):
    """failed rails are never dropped by the allow filter."""
    crashed = FakeRail(executed_actions=[FakeAction(return_value=FakeOutcome(DECISION_BLOCK, failed=True))])
    core(ledger, record_allow=False, include_turn_envelope=False).record_turn([crashed])
    assert len(rows(ledger)) == 1


def test_runtime_and_action_type_are_stamped(ledger):
    core(ledger).record_rail(block_rail())
    c = compute(rows(ledger)[0])
    assert c["runtime"] == "nemoguardrails"
    assert c["observation_mode"] == "rail_log"
    assert rows(ledger)[0]["action_type"] == "fyi"


def test_interaction_id_is_carried_onto_every_capsule_of_the_turn(ledger):
    core(ledger).record_turn([block_rail()], interaction_id="abc-123")
    assert all(compute(r)["nemo_interaction_id"] == "abc-123" for r in rows(ledger))


# --------------------------------------------------------------------------
# the two log-shaped entry points
# --------------------------------------------------------------------------
def test_record_interaction_log_uses_id_and_rails(ledger):
    log = FakeInteractionLog(id="i-9", activated_rails=[block_rail()])
    core(ledger).record_interaction_log(log)
    assert all(compute(r)["nemo_interaction_id"] == "i-9" for r in rows(ledger))


def test_record_generation_response_returns_empty_when_no_log_was_requested(ledger):
    """No options -> no log. That is 'nothing to record', never 'a clean turn'."""
    response = types.SimpleNamespace(response="hi", log=None)
    assert core(ledger).record_generation_response(response) == []
    assert rows(ledger) == []


def test_record_generation_response_seals_the_turn(ledger):
    response = types.SimpleNamespace(
        response="refused", log=types.SimpleNamespace(activated_rails=[block_rail()])
    )
    core(ledger).record_generation_response(response, turn_input=[{"role": "user", "content": "x"}])
    assert len(rows(ledger)) == 3
    assert verdict(rows(ledger)[-1]) == "blocked"


# --------------------------------------------------------------------------
# the in-flow registered action
# --------------------------------------------------------------------------
def test_seal_rail_decision_records_and_returns_observation_only(ledger):
    out = asyncio.run(core(ledger).seal_rail_decision(rail="my rail", decision="block", rail_type="input"))
    assert out["observation_only"] is True
    assert out["decision"] == DECISION_BLOCK
    assert out["capsule_id"] == rows(ledger)[0]["capsule_id"]
    assert verdict(rows(ledger)[0]) == "blocked"


def test_seal_rail_decision_accepts_a_bool(ledger):
    out = asyncio.run(core(ledger).seal_rail_decision(rail="r", decision=True))
    assert out["decision"] == DECISION_BLOCK
    out = asyncio.run(core(ledger).seal_rail_decision(rail="r", decision=False))
    assert out["decision"] == DECISION_ALLOW


def test_seal_rail_decision_accepts_a_rail_outcome(ledger):
    out = asyncio.run(core(ledger).seal_rail_decision(rail="r", decision=FakeOutcome(DECISION_TRANSFORM)))
    assert out["decision"] == DECISION_TRANSFORM


def test_seal_rail_decision_calls_an_unrecognised_decision_unknown(ledger):
    out = asyncio.run(core(ledger).seal_rail_decision(rail="r", decision="something else"))
    assert out["decision"] == DECISION_UNKNOWN


def test_seal_rail_decision_never_raises_on_a_broken_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    c = NeMoRailsCore(operator="o", developer="d", ledger=str(blocker / "l.jsonl"))
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        out = asyncio.run(c.seal_rail_decision(rail="r", decision="block"))
    assert out["capsule_id"] is None
    assert out["observation_only"] is True


# --------------------------------------------------------------------------
# module hygiene
# --------------------------------------------------------------------------
def test_module_imports_without_nemoguardrails_installed(monkeypatch):
    """The core must be importable and testable without the SDK, like its siblings."""
    import importlib

    for name in list(sys.modules):
        if name == "nemoguardrails" or name.startswith("nemoguardrails."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocked(name, *a, **kw):
        if name == "nemoguardrails" or name.startswith("nemoguardrails."):
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", blocked)
    monkeypatch.delitem(sys.modules, "capsule_emit.adapters.nemo_guardrails", raising=False)
    mod = importlib.import_module("capsule_emit.adapters.nemo_guardrails")
    assert mod.NeMoRailsCore is not None
    with pytest.raises(ImportError):
        mod.register_capsule_log_adapter()


def test_public_names_are_exported():
    import capsule_emit.adapters.nemo_guardrails as m

    for name in m.__all__:
        assert hasattr(m, name), name


# --------------------------------------------------------------------------
# live engine
# --------------------------------------------------------------------------
CONFIG_YAML = """
models:
  - type: main
    engine: fake
    model: fake
rails:
  input:
    flows:
      - self check input
prompts:
  - task: self_check_input
    content: |
      Should the user message be blocked? Answer Yes or No.
      User message: "{{ user_input }}"
"""

TRACING_YAML = CONFIG_YAML + """
tracing:
  enabled: true
  adapters:
    - name: CapsuleEmit
      operator: acme-co
      developer: support-bot@v1
      ledger: LEDGER_PATH
"""


def _rails(yaml_text, completions):
    from nemoguardrails import LLMRails, RailsConfig
    from nemoguardrails.testing import FakeLLMModel

    config = RailsConfig.from_content(yaml_content=yaml_text)
    return LLMRails(config, llm=FakeLLMModel(responses=completions))


@requires_ng
def test_attach_registers_on_a_live_llmrails(ledger):
    """LLMRails.register_action / register_action_param, llmrails.py:1735/:1740."""
    rails = _rails(CONFIG_YAML, ["No", "  express greeting", "Hello!"])
    recorder = NeMoGuardrailsCapsuleRecorder(operator="o", developer="d", ledger=ledger)
    assert recorder.attach(rails) is rails
    registered = rails.runtime.action_dispatcher.registered_actions
    assert RAIL_ACTION_NAME in registered
    assert rails.runtime.registered_action_params[ACTION_PARAM_NAME] is recorder


@requires_ng
def test_live_block_seals_a_chain_from_the_generation_log(ledger):
    from nemoguardrails.rails.llm.options import GenerationOptions

    rails = _rails(CONFIG_YAML, ["Yes"])
    recorder = NeMoGuardrailsCapsuleRecorder(operator="o", developer="d", ledger=ledger)
    messages = [{"role": "user", "content": "do something forbidden"}]
    response = asyncio.run(
        rails.generate_async(
            messages=messages, options=GenerationOptions(log={"activated_rails": True})
        )
    )
    results = recorder.record_generation_response(response, turn_input=messages)
    assert results
    recorded = rows(ledger)
    rail_rows = [r for r in recorded if r["action_id"].startswith("rail:")]
    assert rail_rows, "the engine's activated_rails must produce at least one rail capsule"
    assert any(verdict(r) == "blocked" for r in rail_rows)
    assert verdict(recorded[-1]) == "blocked"
    assert "do something forbidden" not in open(ledger).read()


@requires_ng
def test_live_allow_path_seals_an_executed_turn(ledger):
    from nemoguardrails.rails.llm.options import GenerationOptions

    rails = _rails(CONFIG_YAML, ["No", "  express greeting", "Hello there!"])
    recorder = NeMoGuardrailsCapsuleRecorder(operator="o", developer="d", ledger=ledger)
    response = asyncio.run(
        rails.generate_async(
            messages=[{"role": "user", "content": "hello"}],
            options=GenerationOptions(log={"activated_rails": True}),
        )
    )
    recorder.record_generation_response(response)
    recorded = rows(ledger)
    assert recorded
    assert verdict(recorded[-1]) == "executed"


@requires_ng
def test_live_tracing_adapter_records_with_no_application_code(tmp_path, monkeypatch):
    """Wiring 1: YAML only. LogAdapterConfig is extra='allow' (config.py:240-242)."""
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_ANCHOR", "off")
    led = tmp_path / "traced.jsonl"
    register_capsule_log_adapter()
    rails = _rails(TRACING_YAML.replace("LEDGER_PATH", str(led)), ["Yes"])
    asyncio.run(rails.generate_async(messages=[{"role": "user", "content": "forbidden"}]))
    recorded = rows(led)
    assert recorded, "the tracing adapter must seal without any caller-side call"
    assert any(verdict(r) == "blocked" for r in recorded)
    assert recorded[0]["action_id"].startswith(TURN_ACTION)


@requires_ng
def test_registered_adapter_is_resolvable_by_name():
    from nemoguardrails.tracing.adapters.registry import LogAdapterRegistry

    register_capsule_log_adapter()
    assert LogAdapterRegistry().get(LOG_ADAPTER_NAME) is get_capsule_log_adapter_class()


@requires_ng
def test_log_adapter_ignores_unknown_config_keys():
    """extra='allow' means a typo in config.yml must not take down the app."""
    cls = get_capsule_log_adapter_class()
    adapter = cls(operator="o", developer="d", not_a_real_option=True)
    assert adapter.recorder is not None


@requires_ng
def test_live_in_flow_action_is_executable_from_the_dispatcher(ledger):
    """The registered action runs through their dispatcher, not just as a method."""
    rails = _rails(CONFIG_YAML, ["Yes"])
    recorder = NeMoGuardrailsCapsuleRecorder(operator="o", developer="d", ledger=ledger)
    recorder.attach(rails)
    result, stat = asyncio.run(
        rails.runtime.action_dispatcher.execute_action(
            RAIL_ACTION_NAME, {"rail": "my rail", "decision": "block", "rail_type": "input"}
        )
    )
    assert stat == "success"
    assert result["observation_only"] is True
    assert verdict(rows(ledger)[0]) == "blocked"


@requires_ng
def test_demo_runs_hermetically_and_seals_capsules():
    """An example that is never executed by CI is documentation, not a test.

    Mirrors tests/test_examples_smoke.py: assert exit 0 *and* a non-zero capsule
    count, so a digest failure that silently swallows every emit is caught here
    rather than shipping as a broken example.
    """
    import re
    import subprocess
    from pathlib import Path

    demo = Path(__file__).parent.parent / "examples" / "nemo-guardrails" / "demo.py"
    proc = subprocess.run(
        [sys.executable, str(demo)], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    match = re.search(r"Ledger:\s+(\d+)\s+capsule\(s\)\s+sealed", proc.stdout)
    assert match is not None, proc.stdout
    assert int(match.group(1)) > 0
    assert "none (digest-only)" in proc.stdout
    assert "all capsules verify offline" in proc.stdout


@requires_ng
def test_live_config_py_init_hook_registers_before_adapters_are_built(tmp_path, monkeypatch):
    """The zero-application-code path their tracing docs prescribe.

    A `config.py` in the config directory is exec'd and its `init(app)` called at
    llmrails.py:368-394 — *before* `_log_adapters = create_log_adapters(...)` at
    :416. That ordering is what makes registering the adapter from `init()` work,
    and it is the whole basis of the "no application code" claim on the docs page.
    """
    from nemoguardrails import LLMRails, RailsConfig
    from nemoguardrails.testing import FakeLLMModel

    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_ANCHOR", "off")
    led = tmp_path / "ledger.jsonl"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        CONFIG_YAML
        + f"""
tracing:
  enabled: true
  adapters:
    - name: CapsuleEmit
      operator: acme-co
      developer: support-bot@v1
      ledger: {led}
"""
    )
    (cfg_dir / "config.py").write_text(
        "from capsule_emit.adapters.nemo_guardrails import register_capsule_log_adapter\n"
        "\n\ndef init(app):\n    register_capsule_log_adapter()\n"
    )
    rails = LLMRails(RailsConfig.from_path(str(cfg_dir)), llm=FakeLLMModel(responses=["Yes"]))
    asyncio.run(rails.generate_async(messages=[{"role": "user", "content": "forbidden thing"}]))

    recorded = rows(led)
    assert recorded, "config.py + config.yml alone must seal the turn"
    assert any(verdict(r) == "blocked" for r in recorded)
    assert "forbidden thing" not in led.read_text()
