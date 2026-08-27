# SPDX-License-Identifier: Apache-2.0
"""[O4-endpoint-consolidation-ship] acceptance: `witness.agentactioncapsule.org`
is the *live default* -- exercised through the public ``seal()``/``emit()``
surface with no ``witness_url``/``CAPSULE_WITNESS_URL`` override, never by
reading ``DEFAULT_TS_URL`` off the module (that only proves the constant is
set correctly, not that the default code path actually resolves to it).

This is the endpoint-consolidation half of
[emit-anchor-disclosure-and-endpoint-consolidation]'s frozen decision
(witness.agentactioncapsule.org canonical): the anchor.* posting path stays
configured-but-dormant (see ``_PENDING_CNAME_TARGETS`` in
``capsule_emit.checkpoint.emit``) so today's actual HTTP dispatch still lands
on the anchor host until the CNAME propagates -- but the *semantic* endpoint
recorded on every ``WitnessRecord`` (and shown in the first-use notice) must
already read ``witness.agentactioncapsule.org``, since that's what a caller,
a status display, or an auditor reading the record sees. Both facts are
pinned here so either drifting silently fails this test.
"""
from __future__ import annotations

import json
import time

from capsule_emit import seal, witness
from capsule_emit.checkpoint.emit import DEFAULT_TS_URL

_WITNESS_HOST = "https://witness.agentactioncapsule.org"
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


def test_default_ts_url_constant_is_the_witness_host():
    # Sanity check on the constant itself -- NOT the acceptance assertion
    # (see the end-to-end test below for that).
    assert DEFAULT_TS_URL == _WITNESS_HOST


def test_default_witness_endpoint_resolves_to_witness_host_end_to_end(tmp_path, monkeypatch):
    """The acceptance check: call seal() with NO witness_url / CAPSULE_WITNESS_URL
    override, mock the network boundary, and assert on what actually happened
    -- not on a constant."""
    from capsule_emit.checkpoint import emit as emit_mod

    monkeypatch.delenv("CAPSULE_WITNESS_URL", raising=False)
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
    results = [
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger)
        for i in range(5)
    ]
    assert all(r.capsule_id for r in results)

    assert _wait_for(lambda: len(captured) >= 1), "no checkpoint was ever dispatched"

    key = witness._resolve_key(str(ledger))
    assert _wait_for(
        lambda: key in witness._states
        and witness._states[key].prev is not None
        and witness._states[key].prev.witnesses
    ), "CheckpointRecord never recorded its WitnessRecord"

    witness_record = witness._states[key].prev.witnesses[0]

    # The semantic endpoint -- what a caller/status-display/auditor sees --
    # is witness.agentactioncapsule.org, resolved by the default path with
    # zero configuration, not read off a constant.
    assert witness_record.ts_url == _WITNESS_HOST, (
        "the default witness endpoint must resolve to witness.agentactioncapsule.org "
        "with no witness_url override -- it did not"
    )

    # Today, the actual bytes still land on the anchor host: the witness.*
    # CNAME has not propagated (see _PENDING_CNAME_TARGETS). This is the
    # documented, dormant-but-configured indirection -- pin it too, so its
    # removal (once the CNAME goes live) is a deliberate test update, not a
    # silent behavior change.
    assert captured[0]["full_url"] == f"{_TODAY_DISPATCH_HOST}/checkpoints", (
        "expected today's CNAME-pending indirection to the anchor host; if "
        "this now fails because the request went straight to "
        "witness.agentactioncapsule.org, the CNAME has propagated -- update "
        "this test (and retire _PENDING_CNAME_TARGETS) rather than loosen it"
    )

    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False


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
