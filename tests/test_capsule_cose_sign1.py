# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [capsule-cose-sign1] — the draft-04 reversal.

FROZEN PROFILE (2026-08-24): ``capsule_id`` returns to a pure,
signer-independent content address (excludes only ``capsule_id`` itself;
``chain`` and ``canonicalization_id`` are committed under the declared
``"jcs"`` algorithm — closing the prior chain-unauthenticated gap). The
producer signature moves into a COSE_Sign1 envelope over the raw 32-byte
``capsule_id`` digest, reusing ``scitt_cose`` for the COSE/CBOR machinery.

Covers the MANAGER PRE-REWORK FLAGS test-surface narrowing (flag 4):
  (a) regression-confirm: single-producer chains + ledger dedup unchanged;
  (b) two signers, same content -> ONE shared capsule_id (test_seal_signing.py);
  (c) the one pinned edge: a per-event uniqueness field makes two distinct
      seal() calls content-distinct, and a genuine single-signer collision
      is defined-and-intended idempotency, not a silent drop;
and the closed chain-authentication gap (a tampered ``parent_capsule_id`` /
``relation`` must now invalidate the producer signature).
"""
from __future__ import annotations

from capsule_emit import seal, verify_capsule_signature
from capsule_emit.canonicalization import compute_capsule_id


def test_tampered_parent_capsule_id_invalidates_signature(tmp_path, monkeypatch):
    """Closes the pre-reversal chain-auth gap: canonical_capsule_bytes used
    to exclude ``chain`` even under the declared 'jcs' profile, so
    ``parent_capsule_id``/``relation`` were unauthenticated (lineage
    forgeable) even though the capsule carried a valid signature. Under
    'jcs', chain is committed, so retargeting the parent now changes
    capsule_id -- and the stale carried capsule_id/signature no longer
    match the (now-different) recomputed content, so the forged record
    fails signature verification."""
    monkeypatch.chdir(tmp_path)
    parent = seal({"amount": 1}, anchor=False, witness=False)
    child = seal(
        {"amount": 2}, anchor=False, witness=False, confirms=parent.capsule_id
    ).capsule
    assert verify_capsule_signature(child)

    forged = dict(child)
    forged["chain"] = dict(forged["chain"], parent_capsule_id="f" * 64)
    # A naive forger who does not also recompute capsule_id/re-sign is
    # caught by the stale carried capsule_id (structural check territory);
    # the crucial property this closes is that even the SIGNATURE-CHECK
    # layer independently rejects it once capsule_id is recomputed:
    assert compute_capsule_id(forged) != forged["capsule_id"], (
        "chain must be committed into the capsule_id preimage under 'jcs'"
    )
    assert not verify_capsule_signature(forged)

    # A forger who ALSO patches capsule_id to match the tampered chain
    # (but cannot re-sign without the real producer key) still fails --
    # the whole point of a producer signature.
    forged["capsule_id"] = compute_capsule_id(forged)
    assert not verify_capsule_signature(forged)


def test_tampered_relation_invalidates_signature(tmp_path, monkeypatch):
    """Same gap, the other chain field: relation is also committed."""
    monkeypatch.chdir(tmp_path)
    parent = seal({"amount": 1}, anchor=False, witness=False)
    child = seal(
        {"amount": 2}, anchor=False, witness=False,
        confirms=parent.capsule_id, relation="confirms",
    ).capsule
    assert verify_capsule_signature(child)

    forged = dict(child)
    forged["chain"] = dict(forged["chain"], relation="supersedes")
    assert compute_capsule_id(forged) != forged["capsule_id"]
    assert not verify_capsule_signature(forged)


def test_single_producer_chain_regression_unchanged(tmp_path, monkeypatch):
    """[FLAG 4(a)] regression-confirm: an HONEST single-producer chain is
    completely unaffected by the reversal — parent/child capsule_ids,
    chain linkage, and signature verification all behave exactly as
    before (Ed25519 is deterministic, so within one producer nothing about
    the *shape* of a legitimate chain changes; only a forged/tampered
    chain link is now caught that previously was not)."""
    monkeypatch.chdir(tmp_path)
    parent = seal({"amount": 1}, anchor=False, witness=False)
    child = seal(
        {"amount": 2}, anchor=False, witness=False, confirms=parent.capsule_id,
        verdict="confirmed",
    )

    assert child.capsule["chain"]["parent_capsule_id"] == parent.capsule_id
    assert child.capsule["chain"]["relation"] == "confirms"
    assert verify_capsule_signature(parent.capsule)
    assert verify_capsule_signature(child.capsule)
    assert compute_capsule_id(parent.capsule) == parent.capsule_id
    assert compute_capsule_id(child.capsule) == child.capsule_id


def test_ledger_dedup_by_capsule_id_unchanged(tmp_path, monkeypatch):
    """[FLAG 4(a)] regression-confirm: ledger append/read-back by
    capsule_id is unaffected -- each seal() still mints a distinct,
    re-findable leaf keyed by its own capsule_id."""
    from capsule_emit.ledger import read_ledger

    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    results = [
        seal({"amount": i}, anchor=False, witness=False, ledger=ledger)
        for i in range(3)
    ]
    records = read_ledger(ledger)
    assert [r["capsule_id"] for r in records] == [r.capsule_id for r in results]
    assert len(set(r.capsule_id for r in results)) == 3, "each seal() is a distinct leaf"


def test_content_collision_within_one_producer_is_idempotent_not_dropped(tmp_path, monkeypatch):
    """[FLAG 4(c)] pin the edge: if a producer ever DID emit byte-identical
    capsule content twice (same action_id, same timestamp, same everything
    -- not how seal() normally behaves, since action_id/timestamp are
    per-event uniqueness fields, see test_two_events_are_content_distinct
    below), the SAME capsule_id and, for the SAME signer key, the exact
    same signature is the defined, intended outcome (Ed25519 determinism)
    -- content-addressing idempotency, not a silent drop: both appended
    ledger entries independently verify."""
    monkeypatch.chdir(tmp_path)
    base = seal({"amount": 1}, anchor=False, witness=False).capsule
    # Simulate re-minting byte-identical content with the SAME signer/key:
    # same capsule_id, same envelope (Ed25519 is deterministic, RFC 8032).
    from capsule_emit.signing import resolve_signer, sign_producer_envelope

    signer = resolve_signer(str(tmp_path / "ledger.jsonl"))
    envelope_hex, key_id = sign_producer_envelope(signer, base["capsule_id"])
    assert envelope_hex == base["signature"]
    assert key_id == base["key_id"]
    assert verify_capsule_signature(dict(base, signature=envelope_hex, key_id=key_id))


def test_two_events_are_content_distinct(tmp_path, monkeypatch):
    """[FLAG 4(c)] the per-event uniqueness field: two genuinely distinct
    seal() calls -- even with identical caller-supplied agent_input -- get
    distinct action_id/timestamp fields, so they are content-distinct and
    get distinct capsule_ids. A silent collision/drop across unrelated
    events cannot happen."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    first = seal({"amount": 1}, anchor=False, witness=False, ledger=ledger)
    second = seal({"amount": 1}, anchor=False, witness=False, ledger=ledger)
    assert first.capsule["action_id"] != second.capsule["action_id"]
    assert first.capsule_id != second.capsule_id
