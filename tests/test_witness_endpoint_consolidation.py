# SPDX-License-Identifier: Apache-2.0
"""There is no default witness: ``seal()``/``emit()`` with no ``witness_url``
and no ``CAPSULE_WITNESS_URL`` sends nothing anywhere, and says so once. A
configured witness is reached at, and recorded under, exactly its configured
URL. Exercised through the public ``seal()`` surface with the network boundary
mocked, not by reading constants.
"""
from __future__ import annotations

import json
import time

import pytest

from capsule_emit import seal, witness

_TODAY_DISPATCH_HOST = "https://anchor.agentactioncapsule.org"


class _FakeUrlopenResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_response() -> _FakeUrlopenResponse:
    body = json.dumps(
        {
            "entry_hash": "e" * 64,
            "receipt_b64": "c3R1Yg==",
            "leaf_index": 0,
            "tree_size": 1,
        }
    ).encode()
    return _FakeUrlopenResponse(body)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(0.01)
        ok = predicate()
    return ok


def _reset_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False


def test_no_witness_configured_sends_nothing(tmp_path, monkeypatch, capsys):
    from capsule_emit.checkpoint import emit as emit_mod

    monkeypatch.delenv("CAPSULE_WITNESS_URL", raising=False)
    monkeypatch.delenv("CAPSULE_WITNESS", raising=False)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    _reset_witness_state()

    def fake_urlopen(req, timeout=None):
        raise AssertionError(f"nothing may be sent with no witness configured: {req.full_url}")

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    ledger = tmp_path / "ledger.jsonl"
    results = [seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger) for i in range(5)]
    assert {r.witness_outcome for r in results} == {"local_sealed"}
    assert witness.push(str(ledger)) is None
    time.sleep(0.2)  # no background dispatch either
    key = witness._resolve_key(str(ledger))
    assert key not in witness._states or witness._states[key].prev is None
    err = capsys.readouterr().err
    assert "no witness is configured" in err
    assert err.count("no witness is configured") == 1
    _reset_witness_state()


def test_require_witness_with_no_witness_configured_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPSULE_WITNESS_URL", raising=False)
    monkeypatch.delenv("CAPSULE_WITNESS", raising=False)
    _reset_witness_state()
    with pytest.raises(witness.WitnessRequiredError, match="no witness is configured"):
        seal(None, action="a", operator="acme", anchor=False, require_witness=True, ledger=tmp_path / "l.jsonl")
    _reset_witness_state()


def test_the_public_witness_is_reached_at_its_own_url_when_configured(tmp_path, monkeypatch):
    """Configuring the public witness is like configuring any other: the
    request goes to that URL (no rewriting to another host) and the receipt is
    recorded under it."""
    from capsule_emit.checkpoint import emit as emit_mod

    public = "https://witness.agentactioncapsule.org"
    monkeypatch.setenv("CAPSULE_WITNESS_URL", public)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    _reset_witness_state()
    captured: list[str] = []

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _fake_response()

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)
    ledger = tmp_path / "ledger.jsonl"
    for i in range(5):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger)
    assert _wait_for(lambda: len(captured) >= 1), "no checkpoint was ever dispatched"
    key = witness._resolve_key(str(ledger))
    assert _wait_for(
        lambda: key in witness._states
        and witness._states[key].prev is not None
        and witness._states[key].prev.witnesses
    )
    assert witness._states[key].prev.witnesses[0].ts_url == public
    _reset_witness_state()


def test_rollback_to_anchor_host_still_works_via_explicit_config(tmp_path, monkeypatch):
    """[O4] rollback path: if witness.* misbehaves once live, ops repoints
    CAPSULE_WITNESS_URL back at the anchor host by CONFIG -- no code revert
    needed. Assert that explicit override is honored (never rewritten), the
    same guarantee `register_checkpoint`'s non-default path already gives."""
    from capsule_emit.checkpoint import emit as emit_mod

    monkeypatch.setenv("CAPSULE_WITNESS_URL", _TODAY_DISPATCH_HOST)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")

    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False

    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append({"full_url": req.full_url})
        return _fake_response()

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    ledger = tmp_path / "ledger.jsonl"
    for i in range(5):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger)

    assert _wait_for(lambda: len(captured) >= 1), "no checkpoint was ever dispatched"
    assert captured[0]["full_url"] == f"{_TODAY_DISPATCH_HOST}/checkpoints"

    key = witness._resolve_key(str(ledger))
    assert _wait_for(
        lambda: key in witness._states
        and witness._states[key].prev is not None
        and witness._states[key].prev.witnesses
    )
    witness_record = witness._states[key].prev.witnesses[0]
    assert witness_record.ts_url == _TODAY_DISPATCH_HOST, (
        "an explicit CAPSULE_WITNESS_URL override must be recorded verbatim, "
        "not rewritten to the semantic witness host -- this is the config-only "
        "rollback lever"
    )

    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
