# SPDX-License-Identifier: Apache-2.0
"""ADK adapter tests — synthetic harness (no live LLM).

Exercises the full surface the adapter offers, and round-trips every capsule
through the reference verifier:
- after_tool_callback -> executed capsule, verifies
- emit_blocked / emit_denied -> blocked/denied capsules, verify
- event-stream tap pairs calls+responses by id under ParallelAgent-style interleave
- tool_context hygiene: agent_name recorded, session/user_id never serialized
- no effect asserted by default (read-only calls don't manufacture effects)
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from agent_action_capsule import verify

from capsule_emit.adapters.adk import ADKCapsuleEmitter

# --------------------------------------------------------------------------
# Fakes (stand in for ADK objects; no model / Runner needed)
# --------------------------------------------------------------------------

class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    # carries end-user identifiers — MUST NOT reach the capsule
    user_id = "user-SHOULD-NOT-LEAK"
    session_id = "session-SHOULD-NOT-LEAK"


class _FakeToolContext:
    def __init__(self, function_call_id="fc-123"):
        self.agent_name = "writer"
        self.function_call_id = function_call_id
        self.invocation_id = "inv-456"
        self.session = _FakeSession()


class _FC:
    def __init__(self, name, id, args):
        self.name, self.id, self.args = name, id, args


class _FR:
    def __init__(self, name, id, response):
        self.name, self.id, self.response = name, id, response


class _FakeEvent:
    def __init__(self, calls=(), responses=()):
        self._calls, self._responses = list(calls), list(responses)

    def get_function_calls(self):
        return self._calls

    def get_function_responses(self):
        return self._responses


class _Part:
    """A content part carrying either a function_call or function_response."""

    def __init__(self, function_call=None, function_response=None):
        self.function_call = function_call
        self.function_response = function_response


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _PartsEvent:
    """An event with NO get_function_calls accessor — forces the .content.parts fallback."""

    def __init__(self, parts):
        self.content = _Content(list(parts))


def _emitter(tmp_path, **kw):
    return ADKCapsuleEmitter(
        operator="acme-co",
        developer="po-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        model={"provider": "google", "model_id": "gemini-2.0-flash"},
        **kw,
    )


# --------------------------------------------------------------------------
# Path 1 — callbacks
# --------------------------------------------------------------------------

def test_after_tool_callback_emits_executed_and_verifies(tmp_path):
    e = _emitter(tmp_path)
    ret = e.after_tool_callback(
        _FakeTool("lookup_vendor"),
        {"vendor": "Frobozz Supply"},
        _FakeToolContext(),
        {"tier": "gold"},
    )
    assert ret is None  # never overrides the tool response
    cap = e.last.capsule
    assert cap["disposition"]["verdict_class"] == "executed"
    assert verify(cap).ok


def test_blocked_and_denied_verify(tmp_path):
    e = _emitter(tmp_path)
    b = e.emit_blocked(_FakeTool("write_order"), {"total": 1240}, _FakeToolContext(), reason="policy")
    d = e.emit_denied(_FakeTool("write_order"), {"total": 1240}, _FakeToolContext(), reason="authz")
    assert b.capsule["disposition"]["verdict_class"] == "blocked"
    assert d.capsule["disposition"]["verdict_class"] == "denied"
    assert verify(b.capsule).ok
    assert verify(d.capsule).ok


def test_no_effect_asserted_by_default(tmp_path):
    e = _emitter(tmp_path)
    e.after_tool_callback(_FakeTool("get_price"), {"sym": "MSFT"}, _FakeToolContext(), {"px": 470})
    # a read-only tool call must not manufacture a dispatched effect
    assert not e.last.capsule.get("effect")


def test_context_hygiene_records_agent_but_never_session(tmp_path):
    e = _emitter(tmp_path)
    e.after_tool_callback(_FakeTool("t"), {"x": 1}, _FakeToolContext(), {"ok": True})
    cap = e.last.capsule
    blob = json.dumps(cap)
    # allow-listed, non-identifying context is recorded ...
    assert "writer" in blob and "fc-123" in blob
    # ... but nothing from `session` (user_id / session_id) ever reaches the capsule
    assert "SHOULD-NOT-LEAK" not in blob
    assert verify(cap).ok


# --------------------------------------------------------------------------
# Path 2 — event-stream tap, incl. ParallelAgent-style interleave
# --------------------------------------------------------------------------

def test_event_tap_pairs_calls_and_responses_out_of_order(tmp_path):
    e = _emitter(tmp_path)
    # two concurrent tool calls issued together (one ParallelAgent turn)...
    e.tap_event(_FakeEvent(calls=[_FC("a_tool", "idA", {"p": 1}), _FC("b_tool", "idB", {"p": 2})]))
    # ...responses arrive in the OPPOSITE order (B before A), as concurrency allows
    e.tap_event(_FakeEvent(responses=[_FR("b_tool", "idB", {"r": "B"})]))
    e.tap_event(_FakeEvent(responses=[_FR("a_tool", "idA", {"r": "A"})]))

    caps = [r.capsule for r in e.results]
    assert len(caps) == 2
    names = {c["action_id"].split("/", 1)[0] for c in caps}
    assert names == {"a_tool", "b_tool"}
    assert all(verify(c).ok for c in caps)


def test_tap_stream_sync_drains_all(tmp_path):
    e = _emitter(tmp_path)
    events = [
        _FakeEvent(calls=[_FC("t", "id1", {"a": 1})]),
        _FakeEvent(responses=[_FR("t", "id1", {"done": True})]),
    ]
    assert e.tap_stream(events) is None  # sync path returns None
    assert len(e.results) == 1
    assert verify(e.last.capsule).ok


def _input_digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_event_tap_idless_concurrent_same_tool_no_collision(tmp_path):
    """Two concurrent id-LESS calls to the SAME tool must not collide or drop.

    With a single name-keyed pending slot, the second call would overwrite the
    first and one input digest would be lost. The per-name FIFO queue keeps both.
    """
    e = _emitter(tmp_path)
    e.tap_event(_FakeEvent(calls=[_FC("search", None, {"q": "first"}),
                                  _FC("search", None, {"q": "second"})]))
    e.tap_event(_FakeEvent(responses=[_FR("search", None, {"r": 1})]))
    e.tap_event(_FakeEvent(responses=[_FR("search", None, {"r": 2})]))

    caps = [r.capsule for r in e.results]
    assert len(caps) == 2  # no silent drop
    blob = json.dumps(caps)
    # BOTH inputs survived (FIFO pairing) — the collision bug would lose "first"
    assert _input_digest({"q": "first"}) in blob
    assert _input_digest({"q": "second"}) in blob
    assert all(verify(c).ok for c in caps)


# --------------------------------------------------------------------------
# Version tolerance — the `.content.parts` fallback (no accessor methods)
# --------------------------------------------------------------------------

def test_event_tap_parts_fallback_pairs_and_verifies(tmp_path):
    """Events lacking get_function_calls() must be parsed via content.parts."""
    e = _emitter(tmp_path)
    e.tap_event(_PartsEvent([_Part(function_call=_FC("write_order", "idX", {"total": 12}))]))
    e.tap_event(_PartsEvent([_Part(function_response=_FR("write_order", "idX", {"po": "PO-1"}))]))

    assert len(e.results) == 1
    cap = e.last.capsule
    assert cap["action_id"].split("/", 1)[0] == "write_order"
    assert "idX" in json.dumps(cap)  # correlation id threaded through
    assert verify(cap).ok


# --------------------------------------------------------------------------
# Async tap_stream — the previously-untested `_drain` branch
# --------------------------------------------------------------------------

def test_tap_stream_async_drains_all(tmp_path):
    e = _emitter(tmp_path)

    async def _events():
        yield _FakeEvent(calls=[_FC("t", "id1", {"a": 1})])
        yield _FakeEvent(responses=[_FR("t", "id1", {"done": True})])

    awaitable = e.tap_stream(_events())  # async iterable -> returns a coroutine
    assert awaitable is not None
    asyncio.run(awaitable)
    assert len(e.results) == 1
    assert verify(e.last.capsule).ok


def test_event_tap_orphan_response_still_verifies(tmp_path):
    """A response with no matching call pairs to None input but still seals."""
    e = _emitter(tmp_path)
    e.tap_event(_FakeEvent(responses=[_FR("t", "idOrphan", {"r": 1})]))
    assert len(e.results) == 1
    assert verify(e.last.capsule).ok


# --------------------------------------------------------------------------
# Effect map — declarative effects for consequential tools
# --------------------------------------------------------------------------

def test_effect_map_attaches_effect_for_declared_tool_only(tmp_path):
    e = _emitter(tmp_path, effects={"write_order": {"type": "write_order", "status": "dispatched"}})
    # declared consequential tool carries the effect ...
    e.after_tool_callback(_FakeTool("write_order"), {"total": 12}, _FakeToolContext(), {"po": "PO-1"})
    eff = e.last.capsule.get("effect")
    assert eff and eff["type"] == "write_order" and eff["status"] == "dispatched"
    assert verify(e.last.capsule).ok
    # ... an undeclared read-only tool does not (distinct id so it isn't deduped)
    e.after_tool_callback(
        _FakeTool("get_price"), {"sym": "MSFT"}, _FakeToolContext("fc-price"), {"px": 470}
    )
    assert not e.last.capsule.get("effect")


def test_effect_map_applies_on_event_tap(tmp_path):
    e = _emitter(tmp_path, effects={"write_order": {"type": "write_order", "status": "dispatched"}})
    e.tap_event(_FakeEvent(calls=[_FC("write_order", "id1", {"total": 12})]))
    e.tap_event(_FakeEvent(responses=[_FR("write_order", "id1", {"po": "PO-1"})]))
    assert e.last.capsule["effect"]["type"] == "write_order"


# --------------------------------------------------------------------------
# Double-emit dedup — wiring both paths is now idempotent
# --------------------------------------------------------------------------

def test_both_paths_dedup_by_function_call_id(tmp_path):
    e = _emitter(tmp_path)
    # callback seals the call (tool_context carries function_call_id "fc-123") ...
    e.after_tool_callback(_FakeTool("t"), {"x": 1}, _FakeToolContext(), {"ok": True})
    # ... and the event stream carries the SAME id — must not seal a second time
    e.tap_event(_FakeEvent(calls=[_FC("t", "fc-123", {"x": 1})]))
    e.tap_event(_FakeEvent(responses=[_FR("t", "fc-123", {"ok": True})]))
    assert len(e.results) == 1  # deduped, not double-counted


def test_idless_calls_are_not_deduped(tmp_path):
    """Without an id there is nothing to dedup on — both calls seal (documented)."""
    e = _emitter(tmp_path)
    e.tap_event(_FakeEvent(calls=[_FC("t", None, {"x": 1})]))
    e.tap_event(_FakeEvent(responses=[_FR("t", None, {"ok": True})]))
    e.tap_event(_FakeEvent(calls=[_FC("t", None, {"x": 1})]))
    e.tap_event(_FakeEvent(responses=[_FR("t", None, {"ok": True})]))
    assert len(e.results) == 2


# --------------------------------------------------------------------------
# guard() — a real enforcing before_tool_callback
# --------------------------------------------------------------------------

def test_guard_allows_when_predicate_true(tmp_path):
    e = _emitter(tmp_path)
    before = e.guard(lambda name, args: True)
    assert before(_FakeTool("t"), {"x": 1}, _FakeToolContext()) is None
    assert e.results == []  # nothing sealed when allowed


def test_guard_blocks_and_seals_when_predicate_false(tmp_path):
    e = _emitter(tmp_path)
    before = e.guard(lambda name, args: name != "write_order", reason="policy")
    out = before(_FakeTool("write_order"), {"total": 999}, _FakeToolContext())
    assert out == {"error": "blocked by policy"}  # ADK short-circuit
    assert e.last.capsule["disposition"]["verdict_class"] == "blocked"
    assert verify(e.last.capsule).ok


# --------------------------------------------------------------------------
# emit_errored — Path 1 can't otherwise see a raised tool
# --------------------------------------------------------------------------

def test_emit_errored_seals_executed_with_error_and_no_effect(tmp_path):
    e = _emitter(tmp_path, effects={"write_order": {"type": "write_order", "status": "dispatched"}})
    e.emit_errored(_FakeTool("write_order"), {"total": 12}, ValueError("boom"), _FakeToolContext())
    cap = e.last.capsule
    assert cap["disposition"]["verdict_class"] == "executed"
    # output is committed by digest, not stored raw — assert the digest of the error output
    assert _input_digest({"error": "boom"}) in json.dumps(cap)
    # a raise must NOT claim the effect dispatched (may/did hygiene)
    assert not cap.get("effect")
    assert verify(cap).ok


# --------------------------------------------------------------------------
# Bounded retention — long-lived runners don't grow results without end
# --------------------------------------------------------------------------

def test_results_history_is_bounded(tmp_path):
    e = _emitter(tmp_path, max_results=3)
    for i in range(10):
        e.after_tool_callback(_FakeTool(f"t{i}"), {"i": i}, _FakeToolContext(f"fc-{i}"), {"ok": True})
    assert len(e.results) == 3  # capped to the most recent
    assert e.last.capsule["action_id"].split("/", 1)[0] == "t9"  # last always current


def test_pending_eviction_is_bounded(tmp_path):
    """Unpaired id-bearing calls are evicted past the cap instead of leaking."""
    e = _emitter(tmp_path, max_pending=4)
    for i in range(20):  # calls that never get a response
        e.tap_event(_FakeEvent(calls=[_FC("t", f"id{i}", {"i": i})]))
    assert len(e._pending) <= 4


# --------------------------------------------------------------------------
# Emit-error resilience — the record layer never crashes the tool path
# (same policy as the MCP adapter's _safe_emit)
# --------------------------------------------------------------------------

def _break_emit(emitter):
    def _boom(*a, **kw):
        raise RuntimeError("ledger unavailable")
    emitter.emit_capsule = _boom


def test_callback_emit_failure_warns_never_propagates(tmp_path):
    import pytest

    e = _emitter(tmp_path)
    _break_emit(e)
    with pytest.warns(RuntimeWarning, match="failed to seal"):
        ret = e.after_tool_callback(_FakeTool("t"), {"x": 1}, _FakeToolContext("fc-r1"), {"ok": True})
    assert ret is None
    # a failed emit must NOT mark the id sealed — the event tap can still seal it
    assert "fc-r1" not in e._seen


def test_tap_event_emit_failure_warns_never_propagates(tmp_path):
    import pytest

    e = _emitter(tmp_path)
    _break_emit(e)
    with pytest.warns(RuntimeWarning, match="failed to seal"):
        e.tap_event(_FakeEvent(calls=[_FC("t", "fc-r2", {"x": 1})],
                               responses=[_FR("t", "fc-r2", {"ok": True})]))
    assert "fc-r2" not in e._seen


def test_guard_block_holds_when_seal_fails(tmp_path):
    import pytest

    e = _emitter(tmp_path)
    before = e.guard(lambda name, args: False, reason="policy")
    _break_emit(e)
    with pytest.warns(RuntimeWarning, match="failed to seal"):
        out = before(_FakeTool("write_order"), {"total": 999}, _FakeToolContext())
    # the gate outcome must not depend on the record layer's health
    assert out == {"error": "blocked by policy"}


# --------------------------------------------------------------------------
# Orphan/id-less cross-contamination + dedup completeness + retain-nothing cap
# --------------------------------------------------------------------------

def test_orphan_id_response_never_raids_idless_fifo(tmp_path):
    """An id-bearing orphan response must not steal a pending id-less call's args.

    The id-less caller did everything right — its input must survive to pair with
    its own response, and the orphan seals with unknown (None) input instead.
    """
    e = _emitter(tmp_path)
    # id-LESS call to `search` goes pending in the name FIFO
    e.tap_event(_FakeEvent(calls=[_FC("search", None, {"q": "mine"})]))
    # id-BEARING orphan response for the same tool (its call event was dropped)
    e.tap_event(_FakeEvent(responses=[_FR("search", "idGhost", {"r": "orphan"})]))
    # the id-less call's own response arrives
    e.tap_event(_FakeEvent(responses=[_FR("search", None, {"r": "mine"})]))

    caps = [r.capsule for r in e.results]
    assert len(caps) == 2
    orphan_blob = json.dumps(caps[0])
    idless_blob = json.dumps(caps[1])
    # the orphan sealed WITHOUT the id-less input...
    assert _input_digest({"q": "mine"}) not in orphan_blob
    # ...and the id-less call still pairs with its own args
    assert _input_digest({"q": "mine"}) in idless_blob
    assert all(verify(c).ok for c in caps)


def test_emit_errored_marks_seen_so_tap_skips(tmp_path):
    """A manually-sealed raised call must not be re-sealed by the event tap.

    ADK can surface a tool failure as an error-shaped function_response with the
    same id — without the _seen mark, a both-paths setup would double-count it.
    """
    e = _emitter(tmp_path)
    e.emit_errored(_FakeTool("t"), {"x": 1}, ValueError("boom"), _FakeToolContext("fc-err"))
    assert len(e.results) == 1
    e.tap_event(_FakeEvent(responses=[_FR("t", "fc-err", {"error": "boom"})]))
    assert len(e.results) == 1  # deduped — no second capsule


def test_max_results_zero_retains_nothing(tmp_path):
    """max_results=0 means 'retain nothing', not 'unbounded' (0 is falsy)."""
    e = _emitter(tmp_path, max_results=0)
    e.after_tool_callback(_FakeTool("t"), {"x": 1}, _FakeToolContext("fc-a"), {"ok": 1})
    e.after_tool_callback(_FakeTool("t"), {"x": 2}, _FakeToolContext("fc-b"), {"ok": 2})
    assert e.results == []          # nothing retained
    assert e.last is not None       # `last` tracked independently of the cap
    assert verify(e.last.capsule).ok


# --------------------------------------------------------------------------
# observation_mode — provenance stamp per path (profile compute-attestation)
# --------------------------------------------------------------------------

def test_callback_capsules_stamp_in_path(tmp_path):
    e = _emitter(tmp_path)
    e.after_tool_callback(_FakeTool("t"), {"x": 1}, _FakeToolContext("fc-om1"), {"ok": True})
    assert '"observation_mode": "in_path"' in json.dumps(e.last.capsule) or \
           "in_path" in json.dumps(e.last.capsule)
    assert verify(e.last.capsule).ok


def test_tap_capsules_stamp_event_stream(tmp_path):
    e = _emitter(tmp_path)
    e.tap_event(_FakeEvent(calls=[_FC("t", "fc-om2", {"x": 1})],
                           responses=[_FR("t", "fc-om2", {"ok": True})]))
    assert "event_stream" in json.dumps(e.last.capsule)
    assert verify(e.last.capsule).ok


def test_manual_seals_stamp_in_path(tmp_path):
    e = _emitter(tmp_path)
    b = e.emit_blocked(_FakeTool("t"), {"x": 1}, _FakeToolContext(), reason="policy")
    r = e.emit_errored(_FakeTool("t"), {"x": 1}, ValueError("boom"), _FakeToolContext())
    assert "in_path" in json.dumps(b.capsule)
    assert "in_path" in json.dumps(r.capsule)
