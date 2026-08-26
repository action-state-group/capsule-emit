# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from agent_action_capsule.contracts import InvariantError

from capsule_emit import seal
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# Group: I/O digest invariants
# ---------------------------------------------------------------------------

def test_io_digests_without_model(tmp_ledger):
    cap = seal(
        {"key": "value"},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert len(ca["agent_input_digest"]) == 64
    assert verify(cap.capsule).ok


def test_io_digests_with_model(tmp_ledger):
    cap = seal(
        {"key": "value"},
        action="process",
        operator="org",
        developer="agent@v1",
        agent_output={"result": "done"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca
    assert len(ca["agent_input_digest"]) == 64
    assert len(ca["agent_output_digest"]) == 64
    assert verify(cap.capsule).ok


def test_input_digest_stability(tmp_ledger):
    inp = {"vendor": "Acme", "total": "1200"}
    cap_a = seal(inp, action="order", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap_b = seal(inp, action="order", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    ma_a = cap_a.capsule["model_attestation"]["compute_attestation"]
    ma_b = cap_b.capsule["model_attestation"]["compute_attestation"]
    assert ma_a["agent_input_digest"] == ma_b["agent_input_digest"]


def test_mutating_input_changes_capsule_id(tmp_ledger):
    cap_a = seal({"v": "1"}, action="order", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap_b = seal({"v": "2"}, action="order", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    assert cap_a.capsule_id != cap_b.capsule_id


def test_output_digest_without_input(tmp_ledger):
    cap = seal(
        None,
        action="log",
        operator="org",
        developer="agent@v1",
        agent_output={"status": "ok"},
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_output_digest" in ca
    assert "agent_input_digest" not in ca
    assert verify(cap.capsule).ok


def test_both_digests_null_without_io(tmp_ledger):
    cap = seal(
        None,
        action="noop",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation")
    if ma is not None:
        ca = ma.get("compute_attestation")
        if ca is not None:
            assert "agent_input_digest" not in ca
            assert "agent_output_digest" not in ca
    assert verify(cap.capsule).ok


def test_runtime_committed(tmp_ledger):
    cap = seal(
        None,
        action="run",
        operator="org",
        developer="agent@v1",
        runtime="langchain",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert ca.get("runtime") == "langchain"
    assert verify(cap.capsule).ok


# ---------------------------------------------------------------------------
# Group: chain relation= param
# ---------------------------------------------------------------------------

def test_chain_relation_confirms_default(tmp_ledger):
    parent = seal(None, action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = seal(
        None,
        action="action_b",
        operator="org",
        developer="agent@v1",
        confirms=parent.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["relation"] == "confirms"
    assert verify(cap.capsule).ok


def test_chain_relation_supersedes(tmp_ledger):
    parent = seal(None, action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = seal(
        None,
        action="action_b",
        operator="org",
        developer="agent@v1",
        confirms=parent.capsule_id,
        relation="supersedes",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["relation"] == "supersedes"
    assert verify(cap.capsule).ok


def test_chain_relation_escalates(tmp_ledger):
    parent = seal(None, action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = seal(
        None,
        action="action_b",
        operator="org",
        developer="agent@v1",
        confirms=parent.capsule_id,
        relation="escalates",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["relation"] == "escalates"
    assert verify(cap.capsule).ok


def test_chain_relation_assesses(tmp_ledger):
    parent = seal(None, action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = seal(
        None,
        action="action_b",
        operator="org",
        developer="agent@v1",
        confirms=parent.capsule_id,
        relation="assesses",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["relation"] == "assesses"
    assert verify(cap.capsule).ok


def test_no_chain_when_no_confirms(tmp_ledger):
    cap = seal(None, action="standalone", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    assert "chain" not in cap.capsule
    assert verify(cap.capsule).ok


def test_relation_without_confirms_raises(tmp_ledger):
    with pytest.raises(ValueError, match="requires confirms="):
        seal(
            None,
            action="act",
            operator="org",
            developer="agent@v1",
            relation="supersedes",
            anchor=False,
            ledger=tmp_ledger,
        )


def test_relation_none_keeps_chain_without_confirms_assertion(tmp_ledger):
    """relation=None keeps the chain link but drops the relation assertion.

    The underlying agent_action_capsule library requires chain.relation to be
    a non-empty string (§5.4.4) — it has no concept of an omitted relation on
    an existing chain link, so it falls back to its own generic "sequence"
    default rather than leaving the field empty. relation=None here means
    "the caller isn't asserting a relation" (in particular, not "confirms"),
    not that the sealed capsule ends up with a null/absent relation field.
    """
    parent = seal(None, action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = seal(
        None,
        action="action_b",
        operator="org",
        developer="agent@v1",
        confirms=parent.capsule_id,
        relation=None,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["parent_capsule_id"] == parent.capsule_id
    assert cap.capsule["chain"]["relation"] != "confirms"
    assert verify(cap.capsule).ok


def test_relation_none_does_not_raise_without_confirms(tmp_ledger):
    cap = seal(
        None,
        action="standalone",
        operator="org",
        developer="agent@v1",
        relation=None,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert "chain" not in cap.capsule
    assert verify(cap.capsule).ok


# ---------------------------------------------------------------------------
# Group: disposition params
# ---------------------------------------------------------------------------

def test_human_disposed_capsule(tmp_ledger):
    cap = seal(
        None,
        action="approve",
        operator="org",
        developer="agent@v1",
        human_disposed=True,
        approver="human",
        verdict="confirmed",
        effect={"type": "approve", "status": "confirmed"},
        agent_output={"approved": True},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok
    disp = cap.capsule["disposition"]
    assert disp["human_disposed"] is True
    assert disp["approver"] == "human"


def test_human_disposed_wrong_approver_raises(tmp_ledger):
    with pytest.raises(InvariantError):
        seal(
            None,
            action="approve",
            operator="org",
            developer="agent@v1",
            human_disposed=True,
            approver="policy",
            anchor=False,
            ledger=tmp_ledger,
        )


def test_custom_decision(tmp_ledger):
    cap = seal(
        None,
        action="gate",
        operator="org",
        developer="agent@v1",
        decision="reject",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["disposition"]["decision"] == "reject"
    assert verify(cap.capsule).ok


def test_verdict_blocked_never_dispatch(tmp_ledger):
    cap = seal(
        None,
        action="gate",
        operator="org",
        developer="agent@v1",
        verdict="blocked",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_verdict_assessed_action_type_not_fyi(tmp_ledger):
    """verdict="assessed" is a disposition verb — it must not auto-derive to
    "fyi" (design doc §8 build item 1: without the vocabulary addition,
    action_type silently fell back to "fyi", which is the bug this closes)."""
    cap = seal(
        None,
        action="judge",
        operator="org",
        developer="agent@v1",
        verdict="assessed",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["action_type"] == "decide"
    assert cap.capsule["action_type"] != "fyi"
    assert verify(cap.capsule).ok


def test_judge_verdict_capsule_relation_assesses(tmp_ledger):
    """The daily-judge satellite-capsule shape: a verdict capsule chains to
    its subject with relation="assesses" and verdict="assessed", never
    "executed"/"confirmed" — a detection disposition, not an enforcement one."""
    subject = seal(
        {"turn": "the customer was offered two options"},
        action="handle_turn",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    verdict_cap = seal(
        {"subject": {"capsule_id": subject.capsule_id}, "verdict": "pass"},
        action="judge",
        operator="org",
        developer="judge-agent@v1",
        verdict="assessed",
        confirms=subject.capsule_id,
        relation="assesses",
        model={"provider": "anthropic", "model_id": "claude-judge-1"},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verdict_cap.capsule["chain"]["relation"] == "assesses"
    assert verdict_cap.capsule["chain"]["parent_capsule_id"] == subject.capsule_id
    assert verdict_cap.capsule["disposition"]["verdict_class"] == "assessed"
    assert verdict_cap.capsule["action_type"] == "decide"
    assert verify(verdict_cap.capsule).ok


# ---------------------------------------------------------------------------
# Group: stress — huge/empty/binary-ish/unicode inputs
# ---------------------------------------------------------------------------

def test_huge_agent_input(tmp_ledger):
    cap = seal(
        {"data": "x" * 100_000},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert verify(cap.capsule).ok


def test_empty_agent_input(tmp_ledger):
    cap = seal(
        {},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_empty_string_agent_input(tmp_ledger):
    cap = seal(
        "",
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_none_agent_input_explicit(tmp_ledger):
    cap = seal(
        None,
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation")
    if ma is not None:
        ca = ma.get("compute_attestation", {})
        assert "agent_input_digest" not in ca
    assert verify(cap.capsule).ok


def test_unicode_agent_input(tmp_ledger):
    cap = seal(
        {"text": "日本語​éàü 🔒"},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert len(ca["agent_input_digest"]) == 64
    assert ca["agent_input_digest"].islower()
    assert verify(cap.capsule).ok


def test_binary_like_agent_input(tmp_ledger):
    cap = seal(
        {"data": "\x00\x01\x02\xff"},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_deeply_nested_agent_input(tmp_ledger):
    cap = seal(
        {"a": {"b": {"c": {"d": "deep"}}}},
        action="process",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_list_agent_output(tmp_ledger):
    cap = seal(
        None,
        action="process",
        operator="org",
        developer="agent@v1",
        agent_output=[1, 2, 3, {"key": "val"}],
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_output_digest" in ca
    assert len(ca["agent_output_digest"]) == 64
    assert verify(cap.capsule).ok


def test_large_output_string(tmp_ledger):
    cap = seal(
        None,
        action="process",
        operator="org",
        developer="agent@v1",
        agent_output={"text": "y" * 200_000},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


# ---------------------------------------------------------------------------
# Group: stress — confirms to nonexistent/self/cyclic parent
# ---------------------------------------------------------------------------

def test_confirms_nonexistent_parent(tmp_ledger):
    fake_parent = "a" * 64
    cap = seal(
        None,
        action="follow",
        operator="org",
        developer="agent@v1",
        confirms=fake_parent,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap.capsule["chain"]["parent_capsule_id"] == fake_parent
    result_no_store = verify(cap.capsule)
    assert result_no_store.ok
    info_codes = [f.code for f in result_no_store.findings if f.severity == "info"]
    assert any("chain" in c for c in info_codes)
    result_empty_store = verify(cap.capsule, store=[])
    assert not result_empty_store.ok
    error_codes = [f.code for f in result_empty_store.findings if f.severity == "error"]
    assert any("chain_parent_missing" in c for c in error_codes)


def test_confirms_self_circular(tmp_ledger):
    cap = seal(None, action="first", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap2 = seal(
        None,
        action="self_ref",
        operator="org",
        developer="agent@v1",
        confirms=cap.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap2.capsule["chain"]["parent_capsule_id"] == cap.capsule_id


def test_cyclic_chain_two_nodes(tmp_ledger):
    cap_a = seal(None, action="node_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap_b = seal(
        None,
        action="node_b",
        operator="org",
        developer="agent@v1",
        confirms=cap_a.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    cap_a2 = seal(
        None,
        action="node_a2",
        operator="org",
        developer="agent@v1",
        confirms=cap_b.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap_a.capsule).ok
    assert verify(cap_b.capsule).ok
    assert verify(cap_a2.capsule).ok


# ---------------------------------------------------------------------------
# Group: anchor=False/offline behavior
# ---------------------------------------------------------------------------

def test_anchor_false_does_not_anchor(tmp_ledger):
    cap = seal(None, action="noop", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    assert cap.anchored is False


def test_anchor_false_bad_url_no_error(tmp_ledger):
    cap = seal(
        None,
        action="noop",
        operator="org",
        developer="agent@v1",
        anchor=False,
        anchor_url="http://127.0.0.1:1",
        ledger=tmp_ledger,
    )
    assert cap.anchored is False


def test_anchor_true_with_unreachable_url_does_not_raise(tmp_ledger):
    raised = None
    try:
        cap = seal(
            None,
            action="noop",
            operator="org",
            developer="agent@v1",
            anchor=True,
            anchor_url="http://127.0.0.1:1",
            ledger=tmp_ledger,
        )
        assert cap is not None
    except Exception as exc:
        raised = exc
    assert raised is None, f"seal() raised unexpectedly: {raised!r}"


# ---------------------------------------------------------------------------
# Group: property-based (parametrize style)
# ---------------------------------------------------------------------------

_EMIT_CONFIGS = [
    # (kwargs label, emit kwargs)
    (
        "no_io",
        dict(action="bare", operator="org", developer="agent@v1", verdict="executed"),
    ),
    (
        "with_io",
        dict(
            action="io_act",
            operator="org",
            developer="agent@v1",
            agent_input={"q": "hello"},
            agent_output={"a": "world"},
            verdict="executed",
        ),
    ),
    (
        "with_model",
        dict(
            action="model_act",
            operator="org",
            developer="agent@v1",
            agent_input={"prompt": "test"},
            model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            verdict="executed",
        ),
    ),
    (
        "with_confirm_chain",
        None,  # built dynamically in the test
    ),
    (
        "human_disposed",
        dict(
            action="approve",
            operator="org",
            developer="agent@v1",
            human_disposed=True,
            approver="human",
            verdict="confirmed",
            effect={"type": "approve", "status": "confirmed"},
            agent_output={"ok": True},
        ),
    ),
    (
        "with_effect",
        dict(
            action="dispatch",
            operator="org",
            developer="agent@v1",
            verdict="executed",
            effect={"type": "dispatch", "status": "dispatched"},
            agent_output={"ref": "abc"},
        ),
    ),
]


@pytest.mark.parametrize("label,kwargs", _EMIT_CONFIGS)
def test_every_emit_verifies(label, kwargs, tmp_ledger):
    if label == "with_confirm_chain":
        parent = seal(None, action="parent", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
        cap = seal(
            None,
            action="child",
            operator="org",
            developer="agent@v1",
            confirms=parent.capsule_id,
            verdict="confirmed",
            anchor=False,
            ledger=tmp_ledger,
        )
    else:
        agent_input = kwargs.pop("agent_input", None)
        cap = seal(agent_input, **kwargs, anchor=False, ledger=tmp_ledger)
    result = verify(cap.capsule)
    assert result.ok, [f.detail for f in result.findings if f.severity == "error"]
