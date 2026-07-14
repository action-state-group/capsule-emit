# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.viewer — capsule-native ledger viewer.

Covers:
- _category(): rule_change for root memory_write, acknowledge_info_only for memory_delta, apply_candidate default
- _actor(): authority-grant root, REFUSED label, acted_under_authority, confirms, memory_delta, fallback
- render_table(): empty ledger, refusal marked with ⊛, verify column
- render_html(): contains capsule_id, REFUSED actor, refusal CSS class, gate_checks
- CLI --html: writes file, correct record count
- CLI default view: calls render_table (no html arg), verify column populated
- Works on wicket-style ledger (gate_checks in compute_attestation)
"""
from __future__ import annotations

import io
import json
import os

import pytest

from capsule_emit.viewer import _actor, _category, render_html, render_table

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
# render_html
# ---------------------------------------------------------------------------


def test_render_html_contains_capsule_id():
    caps = [_make_capsule("iiii1234567890ab")]
    html = render_html(caps)
    assert "iiii1234" in html


def test_render_html_refusal_class():
    caps = [_make_capsule("jjjj", verdict="blocked", decision="denied", effect_status="planned")]
    html = render_html(caps)
    assert 'class="refusal"' in html


def test_render_html_no_refusal_class_for_normal():
    caps = [_make_capsule("kkkk")]
    html = render_html(caps)
    assert 'class="refusal"' not in html


def test_render_html_refused_actor_label():
    caps = [
        _make_capsule(
            "llll",
            verdict="blocked",
            decision="denied",
            effect_status="planned",
            chain_relation="acted_under_authority",
            chain_parent="aabbccdd1234",
        )
    ]
    html = render_html(caps)
    assert "REFUSED" in html


def test_render_html_gate_checks():
    checks = [
        {"name": "AmountUnderCap", "passed": True, "reason": None},
        {"name": "VendorKnown", "passed": False, "reason": "unknown vendor"},
    ]
    caps = [_make_capsule("mmmm", gate_checks=checks)]
    html = render_html(caps)
    assert "AmountUnderCap" in html
    assert "VendorKnown" in html
    assert "unknown vendor" in html


def test_render_html_verify_ok():
    caps = [_make_capsule("nnnn")]

    class _FakeResult:
        ok = True
        capsule_id = "nnnn"

    html = render_html(caps, verify_results=[_FakeResult()])
    assert "✓ valid" in html


def test_render_html_verify_fail():
    caps = [_make_capsule("oooo")]

    class _FakeResult:
        ok = False
        capsule_id = "oooo"

    html = render_html(caps, verify_results=[_FakeResult()])
    assert "✗ invalid" in html


def test_render_html_no_external_deps():
    caps = [_make_capsule("pppp")]
    html = render_html(caps)
    # No external stylesheet or script src
    assert "href=" not in html
    assert "src=" not in html


def test_render_html_meta_count():
    caps = [_make_capsule("qqqq"), _make_capsule("rrrr")]
    html = render_html(caps, ledger_path="demo.jsonl")
    assert "2 record(s)" in html


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


def test_cli_html_writes_file(wicket_ledger, tmp_path):
    from capsule_emit.cli import main

    out_path = str(tmp_path / "out.html")
    rc = main(["ledger", "view", str(wicket_ledger), "--html", out_path])
    assert rc == 0
    assert os.path.exists(out_path)
    content = open(out_path).read()
    assert "ssss1234" in content
    assert "tttt1234" in content
    assert 'class="refusal"' in content
    assert "AmountUnderCap" in content


def test_cli_default_view_enhanced(wicket_ledger, capsys):
    from capsule_emit.cli import main

    rc = main(["ledger", "view", str(wicket_ledger)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "⊛" in captured.out  # refusal marker for blocked capsule
    assert "verify" in captured.out


def test_cli_html_empty_ledger(tmp_path):
    from capsule_emit.cli import main

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    out_path = str(tmp_path / "out.html")
    rc = main(["ledger", "view", str(empty), "--html", out_path])
    assert rc == 0
    # File written even for empty ledger
    content = open(out_path).read()
    assert "0 record(s)" in content
