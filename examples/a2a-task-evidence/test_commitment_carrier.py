# SPDX-License-Identifier: Apache-2.0
"""Tests for the A2A carrier of an evidence-commitment/1 (spec section 5.2, V0 to V3).

Not collected by the repo's main pytest run, like ``test_flow.py`` (pinned a2a-sdk venv)::

    pytest examples/a2a-task-evidence/test_commitment_carrier.py -v
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_action_capsule.canonical import jcs  # noqa: E402
from commitment_entry import (  # noqa: E402
    COMMITMENT_EXTENSION_URI,
    ED25519_SPKI,
    check_entry,
    commitment_digest,
    commitment_entry,
    read_commitment,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from server import build_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

URI = "https://node.example/commitment.json"
PRIV = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"a2a commitment carrier test key").digest())
KEY = PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def make_commitment(node_id: str = "node-a", issued_at: str = "2026-11-16T00:00:00Z") -> bytes:
    stmt = b"checkpoint statement"
    doc = {
        "profile": "evidence-commitment/1",
        "node_id": node_id,
        "key_id": KEY.hex(),
        "spki": _b64(ED25519_SPKI + KEY),
        "issued_at": issued_at,
        "checkpoint": {"sha256": hashlib.sha256(stmt).hexdigest(), "bytes": _b64(stmt)},
        "history_cards": [],
    }
    sig = PRIV.sign(jcs(doc))
    doc["sig"] = {
        "alg": "Ed25519",
        "canon": "jcs",
        "value": base64.urlsafe_b64encode(sig).rstrip(b"=").decode(),
    }
    return json.dumps(doc, sort_keys=True).encode()


def served_card(octets: bytes, tmp_path):
    """Serve the example card locally and return (card dict, fetch over the same app)."""
    app = build_app(
        ledger_path=str(tmp_path / "ledger"),
        ts_url="http://127.0.0.1:1",
        commitment=(URI, octets),
    )
    client = TestClient(app)
    card = client.get("/.well-known/agent-card.json").json()

    def fetch(uri: str) -> bytes:
        assert uri.startswith("https://node.example/")
        r = client.get(uri.removeprefix("https://node.example"))
        if r.status_code != 200:
            raise OSError(f"{uri}: {r.status_code}")
        return r.content

    return card, fetch


def test_example_card_commitment_fetched_and_verified(tmp_path):
    octets = make_commitment()
    card, fetch = served_card(octets, tmp_path)
    res = read_commitment(card, fetch)
    assert res.ok, (res.failure, res.detail)
    assert res.octets == octets
    assert res.commitment["node_id"] == "node-a"


def test_card_keeps_task_evidence_extension_unchanged(tmp_path):
    from evidence_extension import EXTENSION_URI

    card, _ = served_card(make_commitment(), tmp_path)
    exts = card["capabilities"]["extensions"]
    assert [e["uri"] for e in exts] == [EXTENSION_URI, COMMITMENT_EXTENSION_URI]
    entry = exts[1]
    assert entry.get("required", False) is False
    assert set(entry["params"]) == {"commitment_uri", "commitment_digest"}
    assert "commitment_uri" not in exts[0]["params"]


def test_publishing_is_off_by_default(tmp_path):
    app = build_app(ledger_path=str(tmp_path / "l"), ts_url="http://127.0.0.1:1")
    card = TestClient(app).get("/.well-known/agent-card.json").json()
    assert check_entry(card) == (None, None)
    res = read_commitment(card, lambda u: pytest.fail("fetched"))
    assert res.failure is None and res.commitment is None and not res.ok


def test_digest_mismatch_is_a_carrier_fact_not_a_rejection(tmp_path):
    octets = make_commitment()
    card, _ = served_card(octets, tmp_path)
    newer = make_commitment(issued_at="2026-11-17T00:00:00Z")  # the node moved on: the pointer is stale
    res = read_commitment(card, lambda u: newer)
    assert res.failure == "digest_mismatch"
    assert res.digest_matches is False
    assert res.commitment is not None and not res.ok  # the octets verify on their own


def test_unreachable_commitment_uri(tmp_path):
    card, _ = served_card(make_commitment(), tmp_path)

    def down(uri: str) -> bytes:
        raise OSError("connection refused")

    res = read_commitment(card, down)
    assert res.failure == "commitment_unreachable" and res.commitment is None


def test_card_claims_contradicting_the_commitment_are_ignored(tmp_path):
    octets = make_commitment(node_id="node-a")
    card, fetch = served_card(octets, tmp_path)
    card["name"] = "node-b"
    card["description"] = "key_id deadbeef, node_id node-b, serves no evidence requests"
    card["provider"] = {"organization": "somebody else", "url": "https://other.example"}
    res = read_commitment(card, fetch)
    assert res.ok
    assert res.commitment["node_id"] == "node-a"  # the commitment wins
    assert res.commitment["key_id"] == KEY.hex()


def test_tampered_octets_fail_signature_even_with_matching_digest(tmp_path):
    doc = json.loads(make_commitment())
    doc["node_id"] = "node-evil"
    bad = json.dumps(doc, sort_keys=True).encode()
    card = {"capabilities": {"extensions": [commitment_entry(URI, bad)]}}
    res = read_commitment(card, lambda u: bad)
    assert res.digest_matches is True
    assert res.failure == "commitment_signature" and not res.ok


def test_staple_digest_mismatch():
    doc = json.loads(make_commitment())
    doc["checkpoint"]["sha256"] = "0" * 64
    doc.pop("sig")
    doc["sig"] = {
        "alg": "Ed25519",
        "canon": "jcs",
        "value": base64.urlsafe_b64encode(PRIV.sign(jcs(doc))).rstrip(b"=").decode(),
    }
    bad = json.dumps(doc).encode()
    card = {"capabilities": {"extensions": [commitment_entry(URI, bad)]}}
    assert read_commitment(card, lambda u: bad).failure == "staple_digest"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(required=True),
        lambda e: e["params"].update(size=3),
        lambda e: e["params"].update(commitment_uri="http://node.example/c.json"),
        lambda e: e["params"].update(commitment_digest="sha256:" + "A" * 64),
        lambda e: e["params"].pop("commitment_digest"),
    ],
)
def test_malformed_entry_is_not_read(mutate):
    entry = commitment_entry(URI, b"x")
    mutate(entry)
    card = {"capabilities": {"extensions": [entry]}}
    res = read_commitment(card, lambda u: pytest.fail("fetched a malformed entry"))
    assert res.failure == "a2a_entry"


def test_commitment_in_another_extensions_params_is_discovery_object():
    card = {
        "capabilities": {
            "extensions": [
                {"uri": "https://example.test/er", "params": {"commitment_uri": URI}},
                commitment_entry(URI, b"x"),
            ]
        }
    }
    assert read_commitment(card, lambda u: pytest.fail("fetched")).failure == "discovery_object"


def test_entry_helper_refuses_bad_inputs():
    with pytest.raises(ValueError):
        commitment_entry("http://x.example/c", b"x")
    with pytest.raises(ValueError):
        commitment_entry(URI, "sha256:xyz")
    assert commitment_entry(URI, b"x")["params"]["commitment_digest"] == commitment_digest(b"x")
    assert copy.deepcopy(commitment_entry(URI, b"x"))["required"] is False


def test_unrelated_extension_with_a_checkpoint_member_coexists():
    card = {
        "capabilities": {
            "extensions": [
                {"uri": "https://example.test/other", "params": {"checkpoint": "x"}},
                commitment_entry(URI, b"x"),
            ]
        }
    }
    entry, bad = check_entry(card)
    assert bad is None and entry["params"]["commitment_uri"] == URI


@pytest.mark.parametrize(
    "card",
    [
        {"capabilities": "invalid"},
        {"capabilities": {"extensions": [None]}},
        {"capabilities": {"extensions": "x"}},
    ],
)
def test_malformed_card_never_raises(card):
    res = read_commitment(card, lambda u: pytest.fail("fetched"))
    assert res.failure == "a2a_entry"


@pytest.mark.parametrize("value", [None, {}, 5])
def test_missing_or_non_string_sig_value_is_a_named_failure(value):
    doc = json.loads(make_commitment())
    if value is None:
        del doc["sig"]["value"]
    else:
        doc["sig"]["value"] = value
    bad = json.dumps(doc).encode()
    card = {"capabilities": {"extensions": [commitment_entry(URI, bad)]}}
    assert read_commitment(card, lambda u: bad).failure == "key_id_spki"


def test_uppercase_key_id_is_refused():
    doc = json.loads(make_commitment())
    doc["key_id"] = doc["key_id"].upper()
    bad = json.dumps(doc).encode()
    card = {"capabilities": {"extensions": [commitment_entry(URI, bad)]}}
    assert read_commitment(card, lambda u: bad).failure == "key_id_spki"
