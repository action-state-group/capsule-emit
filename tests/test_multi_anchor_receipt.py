# SPDX-License-Identifier: Apache-2.0
"""Tests for the multi-anchor receipt demo.

Verifies:
  - One record, two receipts, each verified independently.
  - Receipt verification fails with the wrong log key (mutant test).
  - Partial-reachability: absent receipt is reported as absent, never as pass.
  - Verifier never aggregates receipts into a single pass/fail.
  - verify_demo.py's _verify_receipt_entry handles absent entry correctly.

The capsule_anchor app uses a module-level service singleton (_SERVICE in
router.py), so two create_app() calls in the same process share state.
Tests that need truly independent anchor instances are written against the
signing primitives directly rather than through the HTTP layer.
"""
from __future__ import annotations

import base64
import hashlib
import os

import pytest

# capsule-anchor requires Python >=3.11; skip the entire module gracefully
# on earlier versions rather than failing at import time.
pytest.importorskip("capsule_anchor", reason="capsule-anchor not installed (requires Python >=3.11)")

os.environ.setdefault("CAPSULE_ANCHOR_INSECURE_EPHEMERAL_KEY", "1")
os.environ.setdefault("CAPSULE_ANCHOR_INSECURE_IN_MEMORY", "1")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_pubkey_hex_from_app(client) -> str:
    resp = client.get("/anchor/authority-pubkey")
    assert resp.status_code == 200
    return resp.json()["pubkey_hex"]


def _raw_ed25519_hex_to_pem(pubkey_hex: str) -> bytes:
    raw = bytes.fromhex(pubkey_hex)
    spki_prefix = bytes.fromhex("302a300506032b6570032100")
    der = spki_prefix + raw
    b64 = base64.encodebytes(der).strip().decode("ascii")
    return (
        b"-----BEGIN PUBLIC KEY-----\n"
        + b64.encode("ascii")
        + b"\n-----END PUBLIC KEY-----\n"
    )


def _submit_digest(client, capsule_id: str) -> dict:
    resp = client.post("/v1/digest", json={"capsule_id": capsule_id})
    assert resp.status_code == 200
    return resp.json()


def _verify_receipt_offline(sub: dict, capsule_id: str, log_pem: bytes) -> bool:
    from scitt_cose import verify_receipt
    receipt_bytes = base64.b64decode(sub["receipt_b64"])
    result = verify_receipt(
        receipt_bytes,
        leaf_entry_hex=sub["entry_hash"],
        log_public_key_pem=log_pem,
    )
    return result.ok


def _emit_capsule(tmp_path, action: str = "multi_anchor_test") -> str:
    from capsule_emit import emit
    result = emit(
        action=action,
        operator="test-op",
        developer="test-agent@v0",
        agent_input={"x": 1},
        agent_output={"y": 2},
        model={"provider": "test", "model_id": "test-model"},
        verdict="executed",
        effect={"type": action, "status": "dispatched"},
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
    )
    return result.capsule_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anchor(tmp_path):
    """Single capsule-anchor TestClient (in-memory, ephemeral key)."""
    from capsule_anchor.app import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def capsule_id(tmp_path):
    return _emit_capsule(tmp_path)


# ---------------------------------------------------------------------------
# Acceptance tests: one record, two receipts, verified separately
# ---------------------------------------------------------------------------

class TestTwoReceiptsVerifiedSeparately:
    def test_capsule_submits_to_anchor_and_receipt_verifies(
        self, anchor, capsule_id
    ):
        """A capsule_id submitted to the anchor produces a verifiable receipt."""
        log_pem = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))
        sub = _submit_digest(anchor, capsule_id)
        ok = _verify_receipt_offline(sub, capsule_id, log_pem)
        assert ok, "Receipt from anchor must verify offline"

    def test_two_capsule_ids_produce_independent_receipts(
        self, anchor, tmp_path
    ):
        """Two different capsule_ids each produce their own receipt in the log.

        This simulates the multi-anchor scenario: two capsules (or the same
        capsule in two separate logs) each get distinct leaf indices and
        independent verifiable receipts.
        """
        log_pem = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))

        cid1 = _emit_capsule(tmp_path, action="action_one")
        cid2 = _emit_capsule(tmp_path, action="action_two")
        assert cid1 != cid2

        sub1 = _submit_digest(anchor, cid1)
        sub2 = _submit_digest(anchor, cid2)

        # Different leaf indices — two distinct log entries.
        assert sub1["leaf_index"] != sub2["leaf_index"]
        assert sub1["receipt_b64"] != sub2["receipt_b64"]

        # Each receipt verifies independently.
        ok1 = _verify_receipt_offline(sub1, cid1, log_pem)
        ok2 = _verify_receipt_offline(sub2, cid2, log_pem)
        assert ok1, "First receipt must verify"
        assert ok2, "Second receipt must verify"

    def test_no_aggregate_result(self, anchor, capsule_id, tmp_path):
        """Verification returns per-receipt booleans; there is no single aggregate."""
        log_pem = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))

        sub_a = _submit_digest(anchor, capsule_id)
        sub_b = _submit_digest(anchor, capsule_id)

        ok_a = _verify_receipt_offline(sub_a, capsule_id, log_pem)
        ok_b = _verify_receipt_offline(sub_b, capsule_id, log_pem)

        # The result is a list of per-receipt booleans — no single aggregate.
        statuses = [ok_a, ok_b]
        assert len(statuses) == 2
        assert all(statuses)  # both verify in this scenario


# ---------------------------------------------------------------------------
# Mutant tests — every negative check must fail its mutant
# ---------------------------------------------------------------------------

class TestReceiptMutants:
    def test_wrong_log_key_fails_verification(self, anchor, capsule_id):
        """A receipt does NOT verify against a different Ed25519 key.

        This tests the cross-key binding: a receipt is tied to the specific
        signing key that issued it.  We generate a fresh key pair explicitly
        to avoid the in-process singleton issue.
        """
        from agent_action_capsule.anchor import generate_issuer_keypair

        # Get the receipt from the real anchor.
        log_pem_real = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))
        sub = _submit_digest(anchor, capsule_id)

        # Generate a DIFFERENT key pair — this key never signed anything.
        _, pub_pem_other = generate_issuer_keypair()

        # Sanity: the real key verifies.
        ok_real = _verify_receipt_offline(sub, capsule_id, log_pem_real)
        assert ok_real, "Receipt must verify with the real log key"

        # MUTANT: verify with a different key — must fail.
        from scitt_cose import verify_receipt
        receipt_bytes = base64.b64decode(sub["receipt_b64"])
        result_wrong = verify_receipt(
            receipt_bytes,
            leaf_entry_hex=sub["entry_hash"],
            log_public_key_pem=pub_pem_other,
        )
        assert not result_wrong.ok, (
            "MUTANT MUST FAIL: receipt must not verify under a different log key"
        )

    def test_tampered_receipt_fails_verification(self, anchor, capsule_id):
        """A receipt with a flipped bit does NOT verify."""
        from scitt_cose import verify_receipt
        log_pem = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))
        sub = _submit_digest(anchor, capsule_id)

        receipt_bytes = bytearray(base64.b64decode(sub["receipt_b64"]))
        receipt_bytes[-1] ^= 0xFF  # flip last byte
        result = verify_receipt(
            bytes(receipt_bytes),
            leaf_entry_hex=sub["entry_hash"],
            log_public_key_pem=log_pem,
        )
        assert not result.ok, "MUTANT MUST FAIL: tampered receipt must not verify"

    def test_wrong_capsule_id_entry_hash_fails_verification(
        self, anchor, capsule_id, tmp_path
    ):
        """Receipt for a different capsule_id does NOT verify for this capsule_id."""
        from scitt_cose import verify_receipt

        log_pem = _raw_ed25519_hex_to_pem(_fetch_pubkey_hex_from_app(anchor))

        # Submit a DIFFERENT capsule_id.
        other_id = _emit_capsule(tmp_path, action="other_action")
        assert other_id != capsule_id

        sub_other = _submit_digest(anchor, other_id)

        # Use the receipt for other_id but the entry_hash for our capsule_id — must fail.
        our_entry_hash = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()
        receipt_bytes = base64.b64decode(sub_other["receipt_b64"])
        result = verify_receipt(
            receipt_bytes,
            leaf_entry_hex=our_entry_hash,
            log_public_key_pem=log_pem,
        )
        assert not result.ok, (
            "MUTANT MUST FAIL: receipt for a different capsule_id must not verify "
            "for our capsule_id"
        )


# ---------------------------------------------------------------------------
# Partial-reachability tests
# ---------------------------------------------------------------------------

class TestPartialReachability:
    def test_absent_receipt_reported_as_absent_not_pass(self, capsule_id):
        """An absent receipt entry is ABSENT, never a pass."""
        import pathlib
        import sys
        sys.path.insert(0, str(
            pathlib.Path(__file__).parent.parent /
            "examples" / "multi-anchor-receipt-demo"
        ))
        from verify_demo import _verify_receipt_entry

        # Entry with no receipt_b64 — simulates unreachable anchor at submission.
        entry = {
            "label": "anchor-B",
            "ts_url": "http://127.0.0.1:0",
            "log_pubkey_hex": "",
            "entry_hash": "",
            "leaf_index": None,
            "tree_size": None,
            "receipt_b64": "",
        }
        status = _verify_receipt_entry(capsule_id, entry)
        assert not status["receipt_present"], "absent receipt must not be present"
        assert status["receipt_ok"] is None, (
            "absent receipt must not be ok=True or ok=False"
        )

    def test_absent_receipt_does_not_block_present_receipt(
        self, anchor, capsule_id
    ):
        """anchor-A verified + anchor-B absent: each status is independent."""
        import pathlib
        import sys
        sys.path.insert(0, str(
            pathlib.Path(__file__).parent.parent /
            "examples" / "multi-anchor-receipt-demo"
        ))
        from verify_demo import _verify_receipt_entry

        sub_a = _submit_digest(anchor, capsule_id)

        entry_a = {
            "label": "anchor-A",
            "ts_url": "http://anchor-a",
            "log_pubkey_hex": _fetch_pubkey_hex_from_app(anchor),
            "entry_hash": sub_a["entry_hash"],
            "leaf_index": sub_a["leaf_index"],
            "tree_size": sub_a["tree_size"],
            "receipt_b64": sub_a["receipt_b64"],
        }
        entry_b_absent = {
            "label": "anchor-B",
            "ts_url": "http://127.0.0.1:0",
            "log_pubkey_hex": "",
            "entry_hash": "",
            "leaf_index": None,
            "tree_size": None,
            "receipt_b64": "",
        }

        status_a = _verify_receipt_entry(capsule_id, entry_a)
        status_b = _verify_receipt_entry(capsule_id, entry_b_absent)

        assert status_a["receipt_ok"] is True, "anchor-A receipt must verify"
        assert not status_b["receipt_present"], "anchor-B receipt must be absent"
        # anchor-B being absent must NOT block anchor-A.
        assert status_a["receipt_ok"] is True

    def test_partial_reachability_never_aggregated(self, anchor, capsule_id):
        """Absent receipt must never cause the present receipt to be reported differently."""
        import pathlib
        import sys
        sys.path.insert(0, str(
            pathlib.Path(__file__).parent.parent /
            "examples" / "multi-anchor-receipt-demo"
        ))
        from verify_demo import _verify_receipt_entry

        sub_a = _submit_digest(anchor, capsule_id)

        status_a = _verify_receipt_entry(
            capsule_id,
            {
                "label": "anchor-A",
                "ts_url": "http://anchor-a",
                "log_pubkey_hex": _fetch_pubkey_hex_from_app(anchor),
                "entry_hash": sub_a["entry_hash"],
                "leaf_index": sub_a["leaf_index"],
                "tree_size": sub_a["tree_size"],
                "receipt_b64": sub_a["receipt_b64"],
            },
        )
        status_b = _verify_receipt_entry(
            capsule_id,
            {
                "label": "anchor-B",
                "ts_url": "http://127.0.0.1:0",
                "log_pubkey_hex": "",
                "entry_hash": "",
                "leaf_index": None,
                "tree_size": None,
                "receipt_b64": "",
            },
        )

        # Each status is independent.  There is no combined result.
        assert status_a["receipt_ok"] is True
        assert status_b["receipt_ok"] is None  # absent, not False

        # The "policy" — how to combine these — is left to the caller.
        # Show that the relying party must make this decision, not the verifier.
        statuses = [status_a, status_b]
        verified_count = sum(1 for s in statuses if s["receipt_ok"] is True)
        absent_count = sum(1 for s in statuses if not s["receipt_present"])
        assert verified_count == 1
        assert absent_count == 1
