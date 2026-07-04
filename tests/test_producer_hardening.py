# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from agent_action_capsule import verify
from agent_action_capsule.contracts import InvariantError

from capsule_emit import emit

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
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"key": "value"},
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert len(ca["agent_input_digest"]) == 64
    assert verify(cap.capsule).ok


def test_io_digests_with_model(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"key": "value"},
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
    # salt_digests=False: same input → same digest (deterministic, for explicit opt-out)
    inp = {"vendor": "Acme", "total": "1200"}
    cap_a = emit(action="order", operator="org", developer="agent@v1", agent_input=inp, anchor=False, ledger=tmp_ledger, salt_digests=False)
    cap_b = emit(action="order", operator="org", developer="agent@v1", agent_input=inp, anchor=False, ledger=tmp_ledger, salt_digests=False)
    ma_a = cap_a.capsule["model_attestation"]["compute_attestation"]
    ma_b = cap_b.capsule["model_attestation"]["compute_attestation"]
    assert ma_a["agent_input_digest"] == ma_b["agent_input_digest"]
    # Default (salt_digests=True): same input → DIFFERENT digests (per-emit random salt)
    cap_c = emit(action="order", operator="org", developer="agent@v1", agent_input=inp, anchor=False, ledger=tmp_ledger)
    cap_d = emit(action="order", operator="org", developer="agent@v1", agent_input=inp, anchor=False, ledger=tmp_ledger)
    ma_c = cap_c.capsule["model_attestation"]["compute_attestation"]
    ma_d = cap_d.capsule["model_attestation"]["compute_attestation"]
    assert ma_c["agent_input_digest"] != ma_d["agent_input_digest"], (
        "default salt_digests=True must produce unique digests per call (prevents correlation)"
    )
    assert "digest_salt" in ma_c


def test_mutating_input_changes_capsule_id(tmp_ledger):
    cap_a = emit(action="order", operator="org", developer="agent@v1", agent_input={"v": "1"}, anchor=False, ledger=tmp_ledger)
    cap_b = emit(action="order", operator="org", developer="agent@v1", agent_input={"v": "2"}, anchor=False, ledger=tmp_ledger)
    assert cap_a.capsule_id != cap_b.capsule_id


def test_output_digest_without_input(tmp_ledger):
    cap = emit(
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
    cap = emit(
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
    cap = emit(
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
    parent = emit(action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = emit(
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
    parent = emit(action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = emit(
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
    parent = emit(action="action_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap = emit(
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


def test_no_chain_when_no_confirms(tmp_ledger):
    cap = emit(action="standalone", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    assert "chain" not in cap.capsule
    assert verify(cap.capsule).ok


def test_relation_without_confirms_raises(tmp_ledger):
    with pytest.raises(ValueError, match="requires confirms="):
        emit(
            action="act",
            operator="org",
            developer="agent@v1",
            relation="supersedes",
            anchor=False,
            ledger=tmp_ledger,
        )


# ---------------------------------------------------------------------------
# Group: disposition params
# ---------------------------------------------------------------------------

def test_human_disposed_capsule(tmp_ledger):
    cap = emit(
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
        emit(
            action="approve",
            operator="org",
            developer="agent@v1",
            human_disposed=True,
            approver="policy",
            anchor=False,
            ledger=tmp_ledger,
        )


def test_custom_decision(tmp_ledger):
    cap = emit(
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
    cap = emit(
        action="gate",
        operator="org",
        developer="agent@v1",
        verdict="blocked",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


# ---------------------------------------------------------------------------
# Group: stress — huge/empty/binary-ish/unicode inputs
# ---------------------------------------------------------------------------

def test_huge_agent_input(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"data": "x" * 100_000},
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert verify(cap.capsule).ok


def test_empty_agent_input(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_empty_string_agent_input(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input="",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_none_agent_input_explicit(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input=None,
        anchor=False,
        ledger=tmp_ledger,
    )
    ma = cap.capsule.get("model_attestation")
    if ma is not None:
        ca = ma.get("compute_attestation", {})
        assert "agent_input_digest" not in ca
    assert verify(cap.capsule).ok


def test_unicode_agent_input(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"text": "日本語​éàü 🔒"},
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
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"data": "\x00\x01\x02\xff"},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_deeply_nested_agent_input(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"a": {"b": {"c": {"d": "deep"}}}},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok


def test_list_agent_output(tmp_ledger):
    cap = emit(
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
    cap = emit(
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
    cap = emit(
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
    cap = emit(action="first", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap2 = emit(
        action="self_ref",
        operator="org",
        developer="agent@v1",
        confirms=cap.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    assert cap2.capsule["chain"]["parent_capsule_id"] == cap.capsule_id


def test_cyclic_chain_two_nodes(tmp_ledger):
    cap_a = emit(action="node_a", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    cap_b = emit(
        action="node_b",
        operator="org",
        developer="agent@v1",
        confirms=cap_a.capsule_id,
        anchor=False,
        ledger=tmp_ledger,
    )
    cap_a2 = emit(
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
    cap = emit(action="noop", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
    assert cap.anchored is False


def test_anchor_false_bad_url_no_error(tmp_ledger):
    cap = emit(
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
        cap = emit(
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
    assert raised is None, f"emit() raised unexpectedly: {raised!r}"


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
        parent = emit(action="parent", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)
        cap = emit(
            action="child",
            operator="org",
            developer="agent@v1",
            confirms=parent.capsule_id,
            verdict="confirmed",
            anchor=False,
            ledger=tmp_ledger,
        )
    else:
        cap = emit(**kwargs, anchor=False, ledger=tmp_ledger)
    result = verify(cap.capsule)
    assert result.ok, [f.detail for f in result.findings if f.severity == "error"]
