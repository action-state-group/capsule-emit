# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.viewer — capsule-native ledger viewer.

Covers:
- _category(): rule_change for root memory_write, acknowledge_info_only for memory_delta, apply_candidate default
- _actor(): authority-grant root, REFUSED label, acted_under_authority, confirms, memory_delta, fallback
- render_table(): empty ledger, refusal marked with ⊛, verify column
- CLI default view: calls render_table, verify column populated
- ledger view --html and render_html are gone (moved to capsule-viewer)
- Works on wicket-style ledger (gate_checks in compute_attestation)
"""
from __future__ import annotations

import importlib.util
import io
import json

import pytest

from capsule_emit import viewer
from capsule_emit.viewer import _actor, _category, render_table

# ---------------------------------------------------------------------------
# _category
# ---------------------------------------------------------------------------


def test_category_rule_change():
    assert _category("memory_write", None, "approved") == "rule_change"


def test_category_memory_delta():
    assert _category("spend", "memory_delta", "approved") == "acknowledge_info_only"


def test_category_apply_candidate_default():
    assert _category("spend", None, "approved") == "apply_candidate"
    assert _category("spend", "acted_under_authority", "approved") == "apply_candidate"


# ---------------------------------------------------------------------------
# _actor
# ---------------------------------------------------------------------------


def _cap(**kw) -> dict:
    base: dict = {"operator": "acme-co", "developer": "agent@v1"}
    base.update(kw)
    return base


def test_actor_authority_grant():
    cap = _cap(effect={"type": "memory_write"})
    assert "authority-grant" in _actor(cap, "confirmed", "approved", None, None)


def test_actor_refused():
    cap = _cap()
    result = _actor(cap, "planned", "denied", "acted_under_authority", "aabbccdd1234")
    assert "REFUSED" in result
    assert "aabbccdd" in result


def test_actor_acted_under_authority():
    cap = _cap()
    result = _actor(cap, "confirmed", "approved", "acted_under_authority", "aabbccdd1234")
    assert "↑ grant:aabbccdd" in result
    assert "REFUSED" not in result


def test_actor_confirms():
    cap = _cap()
    result = _actor(cap, "confirmed", "approved", "confirms", "aabbccdd1234")
    assert "dispatched:aabbccdd" in result
    assert "confirmed" in result


def test_actor_memory_delta():
    cap = _cap()
    result = _actor(cap, "confirmed", "approved", "memory_delta", "aabbccdd1234")
    assert "memory-delta" in result


def test_actor_fallback():
    cap = _cap()
    result = _actor(cap, "confirmed", "approved", None, None)
    assert result == "agent@v1"


def test_actor_fallback_no_developer():
    cap = {"operator": "acme-co"}
    result = _actor(cap, "confirmed", "approved", None, None)
    assert result == "agent"


# ---------------------------------------------------------------------------
# Capsule fixture helpers
# ---------------------------------------------------------------------------


def _make_capsule(
    cid: str = "aaaa",
    action: str = "write_po",
    verdict: str = "executed",
    decision: str = "approved",
    effect_type: str = "spend",
    effect_status: str = "confirmed",
    chain_relation: str | None = None,
    chain_parent: str | None = None,
    gate_checks: list | None = None,
) -> dict:
    cap: dict = {
        "capsule_id": cid,
        "action_id": f"{action}/v1",
        "operator": "acme-co",
        "developer": "agent@v1",
        "ts": "2026-07-06T00:00:00Z",
        "effect": {"type": effect_type, "status": effect_status},
        "disposition": {"decision": decision, "verdict_class": verdict},
    }
    if chain_relation or chain_parent:
        cap["chain"] = {"relation": chain_relation, "parent_capsule_id": chain_parent}
    if gate_checks is not None:
        cap.setdefault("model_attestation", {})["compute_attestation"] = {
            "gate_checks": gate_checks
        }
    return cap


# ---------------------------------------------------------------------------
# render_table
# ---------------------------------------------------------------------------


def test_render_table_empty():
    out = io.StringIO()
    render_table([], out=out, path="test.jsonl")
    assert "empty" in out.getvalue()


def test_render_table_refusal_marked():
    caps = [
        _make_capsule("bbbb", verdict="blocked", decision="denied", effect_status="planned"),
    ]
    out = io.StringIO()
    render_table(caps, out=out)
    text = out.getvalue()
    assert "⊛" in text  # refusal marker


def test_render_table_no_refusal_marker_for_executed():
    caps = [_make_capsule("cccc", verdict="executed", decision="approved")]
    out = io.StringIO()
    render_table(caps, out=out)
    text = out.getvalue()
    assert "⊛" not in text


def test_render_table_verify_column_present():
    caps = [_make_capsule("dddd")]
    out = io.StringIO()
    render_table(caps, out=out)
    assert "verify" in out.getvalue()


def test_render_table_verify_ok_shown():
    caps = [_make_capsule("eeee")]

    class _FakeResult:
        ok = True
        capsule_id = "eeee"

    out = io.StringIO()
    render_table(caps, verify_results=[_FakeResult()], out=out)
    assert "✓" in out.getvalue()


def test_render_table_verify_fail_shown():
    caps = [_make_capsule("ffff")]

    class _FakeResult:
        ok = False
        capsule_id = "ffff"

    out = io.StringIO()
    render_table(caps, verify_results=[_FakeResult()], out=out)
    assert "✗" in out.getvalue()


def test_render_table_actor_lineage_label():
    caps = [
        _make_capsule(
            "gggg",
            chain_relation="acted_under_authority",
            chain_parent="aabbccdd1234",
        )
    ]
    out = io.StringIO()
    render_table(caps, out=out)
    assert "↑ grant:aabbccdd" in out.getvalue()


def test_render_table_chain_label_shown():
    caps = [
        _make_capsule(
            "hhhh",
            chain_relation="confirms",
            chain_parent="1234567890ab",
        )
    ]
    out = io.StringIO()
    render_table(caps, out=out)
    text = out.getvalue()
    assert "12345678" in text  # chain parent short


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def wicket_ledger(tmp_path):
    """A JSONL ledger with one passing and one blocked capsule."""
    caps = [
        _make_capsule(
            "ssss1234",
            gate_checks=[{"name": "AmountUnderCap", "passed": True, "reason": None}],
        ),
        _make_capsule(
            "tttt1234",
            verdict="blocked",
            decision="denied",
            effect_status="planned",
            gate_checks=[{"name": "AmountUnderCap", "passed": False, "reason": "over cap"}],
        ),
    ]
    path = tmp_path / "ledger.jsonl"
    with open(path, "w") as fh:
        for cap in caps:
            fh.write(json.dumps(cap) + "\n")
    return path


def test_cli_default_view_enhanced(wicket_ledger, capsys):
    from capsule_emit.cli import main

    rc = main(["ledger", "view", str(wicket_ledger)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "⊛" in captured.out  # refusal marker for blocked capsule
    assert "verify" in captured.out


def test_the_html_pages_moved_to_capsule_viewer(wicket_ledger, capsys):
    """``ledger view --html`` and ``render_html`` are capsule-viewer's
    (``python -m capsule_viewer.ledger``), and so is ``report``; the terminal
    table stays."""
    from capsule_emit.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["ledger", "view", str(wicket_ledger), "--html", "out.html"])
    assert exc.value.code == 2
    assert "--html" in capsys.readouterr().err
    assert not hasattr(viewer, "render_html") and viewer.__all__ == ["render_table"]
    # So did the evidence report page (python -m capsule_viewer.evidence_report).
    with pytest.raises(SystemExit) as exc:
        main(["report", str(wicket_ledger)])
    assert exc.value.code == 2
    assert importlib.util.find_spec("capsule_emit.evidence_report") is None
