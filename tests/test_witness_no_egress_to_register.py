# SPDX-License-Identifier: Apache-2.0
"""No-egress CI gate: the default checkpoint-witness path must NEVER touch
``/register`` -- the witness host's explicit opt-in, plain-digest,
per-record registration route (single-host witness ruling, 2026-08-27).

Privacy here is enforced at the ROUTE level, not a host-level gate: both
``/checkpoints`` (default) and ``/register`` (opt-in) are always reachable on
the same witness deployment. What keeps a default ``capsule-emit`` process's
egress checkpoint-only is this CLIENT-side guarantee -- mirroring the
existing layer-0 import-cost gate (``tests/test_checkpoint_layer0_cost.py``:
"``capsule_emit.checkpoint`` must never load unless a caller explicitly opts
in") with the same discipline applied to network egress instead of imports:
"``/register`` must never be dialed unless a caller explicitly opts in."

Covers:
- the default ``emit()``/``seal()`` witnessing path (cadence-driven checkpoint
  dispatch) never requests a URL containing ``/register``
- the durable witness-outage retry path (``retry_pending_witness_stamps``,
  which also calls ``register_checkpoint``) never does either
- every request the default path makes lands on ``/checkpoints`` specifically
  (a positive assertion -- "never /register" alone would also pass if egress
  silently stopped happening at all)
"""
from __future__ import annotations

import time

import pytest

from capsule_emit import seal, witness


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    yield
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(0.01)
        ok = predicate()
    return ok


def test_default_emit_witness_path_never_requests_register(tmp_path, monkeypatch):
    """The acceptance check: drive a real cadence-crossing ``seal()`` stream
    through the default witness path, with the network boundary mocked, and
    assert on what actually left the process -- not on a constant."""
    from capsule_emit.checkpoint import emit as emit_mod

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")

    requested_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        requested_urls.append(req.full_url)
        raise AssertionError(
            f"stub TS should never actually be dialed in this test double: {req.full_url}"
        )

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    ledger = tmp_path / "ledger.jsonl"
    for i in range(3):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger)

    assert _wait_for(lambda: len(requested_urls) >= 1), "no checkpoint was ever dispatched"

    assert not any("/register" in url for url in requested_urls), (
        f"the default witness path must NEVER touch /register: {requested_urls}"
    )
    assert not any("/v1/digest" in url for url in requested_urls), (
        f"the default witness path must NEVER touch the /register legacy alias either: "
        f"{requested_urls}"
    )
    assert all(url.endswith("/checkpoints") for url in requested_urls), (
        f"every default-path request must land on /checkpoints specifically -- "
        f"a passing 'never /register' check alone would also pass if egress silently "
        f"stopped happening at all: {requested_urls}"
    )


def test_witness_outage_retry_path_never_requests_register(tmp_path, monkeypatch):
    """``retry_pending_witness_stamps`` is the OTHER caller of
    ``register_checkpoint`` (draining the durable backlog on the next real
    emit) -- it must honor the same no-egress-to-/register guarantee as the
    primary dispatch path, not just the happy path above."""
    from capsule_emit.checkpoint import emit as emit_mod

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")

    requested_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        requested_urls.append(req.full_url)
        raise ConnectionRefusedError("simulated witness outage")

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    ledger = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"first-{i}", operator="acme", anchor=False, ledger=ledger)
    assert _wait_for(lambda: len(requested_urls) >= 1), "no checkpoint was ever dispatched"

    # A second cadence crossing triggers the retry-backlog drain (of the
    # first, still-unconfirmed checkpoint) BEFORE handling the new one.
    for i in range(2, 4):
        seal(None, action=f"second-{i}", operator="acme", anchor=False, ledger=ledger)
    assert _wait_for(lambda: len(requested_urls) >= 2)

    assert not any("/register" in url for url in requested_urls), (
        f"the outage-retry path must NEVER touch /register either: {requested_urls}"
    )
    assert all(url.endswith("/checkpoints") for url in requested_urls), requested_urls
