# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.connector: the ConnectorPort contract.

Covers:
- classify_signal_1: every runtime-signal priority order, and the fail-safe
  default when no signal is present.
- ConnectorEvent: name/foreign-field validation.
- MCPCapsuleEmitter and LangChainListenerCore/LangChainCapsuleListener both
  satisfy isinstance(x, ConnectorPort), and their classify()/capture() agree
  with the shared rule and with seal()/received()'s own dispatch rule.
"""
from __future__ import annotations

import pytest

from capsule_emit.connector import (
    BoundaryClass,
    Classification,
    ConnectorEvent,
    ConnectorPort,
    classify_signal_1,
)

# ---------------------------------------------------------------------------
# classify_signal_1
# ---------------------------------------------------------------------------


def test_destructive_hint_true_is_always_effect():
    event = ConnectorEvent(name="wipe", mcp_destructive_hint=True, mcp_read_only_hint=True)
    assert classify_signal_1(event) is Classification.EFFECT


def test_read_only_hint_true_is_observation():
    event = ConnectorEvent(name="get_weather", mcp_read_only_hint=True)
    assert classify_signal_1(event) is Classification.OBSERVATION


def test_read_only_hint_false_is_effect():
    event = ConnectorEvent(name="book_flight", mcp_read_only_hint=False)
    assert classify_signal_1(event) is Classification.EFFECT


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_safe_http_methods_are_observation(method):
    event = ConnectorEvent(name="fetch", http_method=method)
    assert classify_signal_1(event) is Classification.OBSERVATION


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unsafe_http_methods_are_effect(method):
    event = ConnectorEvent(name="mutate", http_method=method)
    assert classify_signal_1(event) is Classification.EFFECT


def test_commit_step_present_decides_when_it_is_the_only_signal():
    assert classify_signal_1(ConnectorEvent(name="a", commit_step_present=True)) is Classification.EFFECT
    assert classify_signal_1(ConnectorEvent(name="b", commit_step_present=False)) is Classification.OBSERVATION


def test_no_signal_present_defaults_to_effect_fail_safe():
    """An event with every signal absent must classify as EFFECT, not
    silently pass as an observation. This is the mutant this test exists to
    catch — flip the default in classify_signal_1 to OBSERVATION and this
    test goes red."""
    event = ConnectorEvent(name="mystery")
    assert classify_signal_1(event) is Classification.EFFECT


def test_signal_priority_destructive_beats_read_only():
    event = ConnectorEvent(name="a", mcp_read_only_hint=True, mcp_destructive_hint=True)
    assert classify_signal_1(event) is Classification.EFFECT


def test_signal_priority_read_only_beats_http_method():
    event = ConnectorEvent(name="a", mcp_read_only_hint=True, http_method="POST")
    assert classify_signal_1(event) is Classification.OBSERVATION


def test_signal_priority_http_method_beats_commit_step():
    event = ConnectorEvent(name="a", http_method="GET", commit_step_present=True)
    assert classify_signal_1(event) is Classification.OBSERVATION


# ---------------------------------------------------------------------------
# ConnectorEvent validation
# ---------------------------------------------------------------------------


def test_empty_name_rejected():
    with pytest.raises(ValueError):
        ConnectorEvent(name="")


def test_foreign_without_bytes_rejected():
    with pytest.raises(ValueError):
        ConnectorEvent(name="a", foreign=True, foreign_type="machine-mandate")


def test_foreign_without_type_rejected():
    with pytest.raises(ValueError):
        ConnectorEvent(name="a", foreign=True, foreign_bytes=b"x")


def test_foreign_with_empty_type_rejected():
    with pytest.raises(ValueError):
        ConnectorEvent(name="a", foreign=True, foreign_bytes=b"x", foreign_type="   ")


def test_foreign_well_formed_accepted():
    event = ConnectorEvent(name="a", foreign=True, foreign_bytes=b"x", foreign_type="machine-mandate")
    assert event.foreign_type == "machine-mandate"


# ---------------------------------------------------------------------------
# MCPCapsuleEmitter — boundary-capture conformance
# ---------------------------------------------------------------------------


def test_mcp_emitter_is_a_connector_port(tmp_path):
    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(operator="acme", developer="agent@v1", ledger=tmp_path / "l.jsonl")
    assert isinstance(emitter, ConnectorPort)
    assert emitter.boundary_class == BoundaryClass.DECORATOR.value


@pytest.mark.parametrize(
    "event",
    [
        ConnectorEvent(name="get_weather", mcp_read_only_hint=True),
        ConnectorEvent(name="book_flight", mcp_read_only_hint=False),
        ConnectorEvent(name="wipe", mcp_destructive_hint=True, mcp_read_only_hint=True),
        ConnectorEvent(name="mystery"),
        ConnectorEvent(name="fetch", http_method="GET"),
        ConnectorEvent(name="mutate", http_method="POST"),
    ],
)
def test_mcp_emitter_classify_matches_shared_rule(tmp_path, event):
    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(operator="acme", developer="agent@v1", ledger=tmp_path / "l.jsonl")
    assert emitter.classify(event) is classify_signal_1(event)


def test_mcp_emitter_capture_seals_an_authored_event(tmp_path):
    from capsule_emit import read_ledger
    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    ledger = tmp_path / "l.jsonl"
    emitter = MCPCapsuleEmitter(operator="acme", developer="agent@v1", ledger=ledger, anchor=False)
    event = ConnectorEvent(name="book_flight", tool_input={"dest": "SFO"}, tool_output={"ok": True})
    result = emitter.capture(event)
    assert result.capsule["action_id"].startswith("book_flight/")
    entries = list(read_ledger(ledger))
    assert len(entries) == 1


def test_mcp_emitter_capture_carries_a_foreign_event(tmp_path):
    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    ledger = tmp_path / "l.jsonl"
    emitter = MCPCapsuleEmitter(operator="acme", developer="agent@v1", ledger=ledger, anchor=False)
    event = ConnectorEvent(name="carried", foreign=True, foreign_bytes=b"their-bytes", foreign_type="machine-mandate")
    result = emitter.capture(event)
    carried = result.capsule["model_attestation"]["compute_attestation"]["carried_artifact"]
    assert carried["type"] == "machine-mandate"


# ---------------------------------------------------------------------------
# LangChainListenerCore / LangChainCapsuleListener — listener conformance
# ---------------------------------------------------------------------------


def test_langchain_listener_core_is_a_connector_port(tmp_path):
    from capsule_emit.adapters.langchain_listener import LangChainListenerCore

    core = LangChainListenerCore(operator="acme", developer="agent@v1", ledger=tmp_path / "l.jsonl")
    assert isinstance(core, ConnectorPort)
    assert core.boundary_class == BoundaryClass.LISTENER.value


def test_langchain_listener_core_capture_seals_confirmed(tmp_path):
    from capsule_emit.adapters.langchain_listener import LangChainListenerCore

    ledger = tmp_path / "l.jsonl"
    core = LangChainListenerCore(operator="acme", developer="agent@v1", ledger=ledger, anchor=False)
    event = ConnectorEvent(name="search", tool_input={"q": "x"}, tool_output={"r": 1})
    result = core.capture(event)
    assert result.capsule["effect"]["status"] == "confirmed"


def test_langchain_listener_core_capture_carries_a_foreign_event(tmp_path):
    from capsule_emit.adapters.langchain_listener import LangChainListenerCore

    ledger = tmp_path / "l.jsonl"
    core = LangChainListenerCore(operator="acme", developer="agent@v1", ledger=ledger, anchor=False)
    event = ConnectorEvent(name="carried", foreign=True, foreign_bytes=b"their-bytes", foreign_type="machine-mandate")
    result = core.capture(event)
    carried = result.capsule["model_attestation"]["compute_attestation"]["carried_artifact"]
    assert carried["type"] == "machine-mandate"


def test_langchain_capsule_listener_delegates_to_core(tmp_path):
    langchain_core = pytest.importorskip("langchain_core")
    del langchain_core
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    ledger = tmp_path / "l.jsonl"
    listener = LangChainCapsuleListener(operator="acme", developer="agent@v1", ledger=ledger, anchor=False)
    assert isinstance(listener, ConnectorPort)
    assert listener.boundary_class == BoundaryClass.LISTENER.value
    event = ConnectorEvent(name="search", tool_input={"q": "x"}, tool_output={"r": 1})
    result = listener.capture(event)
    assert result.capsule["effect"]["status"] == "confirmed"
