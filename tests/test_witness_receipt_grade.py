# SPDX-License-Identifier: Apache-2.0
"""A receipt grade (``-65537`` in the COSE Receipt's protected header) is
reported only for a receipt that is bound to the checkpoint and whose
signature verifies under the key it is checked against.

``scitt_cose.verify_receipt`` fills ``protected_header_ext`` during its
structural decode, before the signature check, so reading the label from
that result without checking ``ok`` lets anyone who can mint a COSE_Sign1
with their own key claim ``mmr-verified`` for a witness. These tests mint
exactly that receipt and require ``None`` for it, and the same for a genuine
receipt replayed from another checkpoint, while a genuine receipt for this
checkpoint keeps its grade -- both with a pinned key and with the key served
at the witness's ``ts_url`` over HTTP.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from scitt_cose import build_receipt, sign_sign1

from capsule_emit.checkpoint import DEFAULT_TS_URL, CheckpointRecord, WitnessRecord
from capsule_emit.witness import CheckpointWitnessState, _receipt_grade

#: COSE Receipt header labels, as scitt_cose.receipt defines them (verifiable
#: data structure / verifiable data proofs), and the private-use receipt-grade
#: label under test.
_HDR_VDS = 395
_HDR_VDP = 396
_VDS_RFC9162_SHA256 = 1
_VDP_INCLUSION_PROOFS = -1
_GRADE_LABEL = -65537


def _checkpoint(mmr_size: int) -> CheckpointRecord:
    return CheckpointRecord(
        v=1,
        kind="mmr_checkpoint",
        log_id="receipt-grade-test-log",
        mmr_size=mmr_size,
        root="11" * 32,
        prev_size=0,
        prev_root="",
        key_id="receipt-grade-test-key",
        timestamp="2026-09-26T00:00:00Z",
        signature="",
    )


def _entry_hash(cp: CheckpointRecord) -> str:
    """The entry hash a Transparency Service records for ``cp``, and what a
    stamp must carry to be bound to it."""
    return hashlib.sha256(bytes.fromhex(cp.digest())).hexdigest()


CHECKPOINT = _checkpoint(mmr_size=4)
OTHER_CHECKPOINT = _checkpoint(mmr_size=7)


def _keypair() -> tuple[bytes, bytes, bytes]:
    """(private PEM, public PEM, raw 32-byte public key) for a fresh Ed25519 key."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return (
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()),
        public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        public_key.public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


WITNESS_PRIV, WITNESS_PUB, WITNESS_RAW = _keypair()
ATTACKER_PRIV, _ATTACKER_PUB, _ATTACKER_RAW = _keypair()


def _receipt_b64(private_key_pem: bytes, grade: str | None, entry_hash: str) -> str:
    """A single-leaf RFC 9162 COSE Receipt over ``entry_hash``, signed with
    ``private_key_pem``, carrying ``grade`` at label -65537 when given. For a
    one-leaf tree the root is the RFC 9162 leaf hash and the audit path is
    empty. Without a grade this is byte-identical to
    ``scitt_cose.build_receipt`` (see
    ``test_hand_built_receipt_matches_build_receipt``); the grade is the
    extra protected label ``build_receipt`` has no parameter for."""
    root = hashlib.sha256(b"\x00" + bytes.fromhex(entry_hash)).digest()
    protected: dict[int, int | str] = {_HDR_VDS: _VDS_RFC9162_SHA256}
    if grade is not None:
        protected[_GRADE_LABEL] = grade
    inclusion_proof = cbor2.dumps([1, 0, []])
    receipt = sign_sign1(
        root,
        alg="EdDSA",
        private_key_pem=private_key_pem,
        protected=protected,
        unprotected={_HDR_VDP: {_VDP_INCLUSION_PROOFS: [inclusion_proof]}},
        detached=True,
    )
    return base64.b64encode(receipt).decode()


def _witness(
    ts_url: str,
    private_key_pem: bytes,
    grade: str | None,
    *,
    receipt_for: CheckpointRecord = CHECKPOINT,
    is_stub: bool = False,
) -> WitnessRecord:
    """A stamp whose entry hash and receipt are both for ``receipt_for``."""
    entry_hash = _entry_hash(receipt_for)
    return WitnessRecord(
        ts_url=ts_url,
        entry_hash=entry_hash,
        receipt_b64=_receipt_b64(private_key_pem, grade, entry_hash),
        leaf_index=0,
        tree_size=1,
        is_stub=is_stub,
    )


class _WitnessPubkeyHandler(BaseHTTPRequestHandler):
    """Serves the witness's real key at the path
    ``verify_receipt_offline(..., ts_base_url=...)`` fetches it from."""

    def do_GET(self) -> None:
        if self.path != "/anchor/authority-pubkey":
            self.send_error(404)
            return
        body = json.dumps({"pubkey_hex": WITNESS_RAW.hex()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- stdlib signature
        pass


def _serve_witness_key() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _WitnessPubkeyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def witness_url() -> Iterator[str]:
    yield from _serve_witness_key()


@pytest.fixture
def second_witness_url() -> Iterator[str]:
    yield from _serve_witness_key()


# -- the test receipts are the shape scitt_cose mints ------------------------


def test_hand_built_receipt_matches_build_receipt() -> None:
    entry_hash = _entry_hash(CHECKPOINT)
    reference = build_receipt(
        leaf_entry_hex=entry_hash,
        leaf_index=0,
        tree_entries_hex=[entry_hash],
        alg="EdDSA",
        log_private_key_pem=WITNESS_PRIV,
    )
    assert base64.b64decode(_receipt_b64(WITNESS_PRIV, None, entry_hash)) == reference


# -- pinned key -------------------------------------------------------------


def test_attacker_signed_mmr_verified_receipt_has_no_grade_under_pinned_key() -> None:
    forged = _witness("https://witness.example", ATTACKER_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, forged, ts_pubkey_pem=WITNESS_PUB) is None


def test_genuine_receipt_keeps_its_grade_under_pinned_key() -> None:
    genuine = _witness("https://witness.example", WITNESS_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, genuine, ts_pubkey_pem=WITNESS_PUB) == "mmr-verified"


def test_pinned_key_applies_to_a_witness_at_the_default_ts_url() -> None:
    # Unpinned, the default ts_url is checked against the library's built-in
    # key; a caller's pin must replace it in the binding check too, or a
    # genuine receipt under the pinned key would lose its grade.
    genuine = _witness(DEFAULT_TS_URL, WITNESS_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, genuine, ts_pubkey_pem=WITNESS_PUB) == "mmr-verified"
    forged = _witness(DEFAULT_TS_URL, ATTACKER_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, forged, ts_pubkey_pem=WITNESS_PUB) is None


def test_genuine_receipt_replayed_from_another_checkpoint_has_no_grade_under_pinned_key() -> None:
    replayed = _witness(
        "https://witness.example", WITNESS_PRIV, "mmr-verified", receipt_for=OTHER_CHECKPOINT
    )
    # The receipt is genuine for OTHER_CHECKPOINT...
    assert _receipt_grade(OTHER_CHECKPOINT, replayed, ts_pubkey_pem=WITNESS_PUB) == "mmr-verified"
    # ...and says nothing about CHECKPOINT.
    assert _receipt_grade(CHECKPOINT, replayed, ts_pubkey_pem=WITNESS_PUB) is None


def test_genuine_receipt_without_label_has_no_grade() -> None:
    unlabelled = _witness("https://witness.example", WITNESS_PRIV, None)
    assert _receipt_grade(CHECKPOINT, unlabelled, ts_pubkey_pem=WITNESS_PUB) is None


def test_genuine_receipt_with_undefined_grade_value_has_no_grade() -> None:
    odd = _witness("https://witness.example", WITNESS_PRIV, "fully-verified")
    assert _receipt_grade(CHECKPOINT, odd, ts_pubkey_pem=WITNESS_PUB) is None


def test_stub_witness_has_no_grade_even_with_genuine_signature() -> None:
    stub = _witness("https://witness.example", WITNESS_PRIV, "mmr-verified", is_stub=True)
    assert _receipt_grade(CHECKPOINT, stub, ts_pubkey_pem=WITNESS_PUB) is None


def test_no_checkpoint_to_bind_to_yields_no_grade() -> None:
    genuine = _witness("https://witness.example", WITNESS_PRIV, "mmr-verified")
    assert _receipt_grade(None, genuine, ts_pubkey_pem=WITNESS_PUB) is None


# -- key served at the witness's own ts_url (the default path) --------------


def test_attacker_signed_receipt_has_no_grade_when_key_fetched_from_ts_url(witness_url: str) -> None:
    forged = _witness(witness_url, ATTACKER_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, forged) is None


def test_genuine_receipt_keeps_its_grade_when_key_fetched_from_ts_url(witness_url: str) -> None:
    genuine = _witness(witness_url, WITNESS_PRIV, "countersigned-observed")
    assert _receipt_grade(CHECKPOINT, genuine) == "countersigned-observed"


def test_genuine_receipt_replayed_from_another_checkpoint_has_no_grade_when_key_fetched(
    witness_url: str,
) -> None:
    replayed = _witness(witness_url, WITNESS_PRIV, "mmr-verified", receipt_for=OTHER_CHECKPOINT)
    assert _receipt_grade(CHECKPOINT, replayed) is None


def test_unreachable_witness_key_yields_no_grade() -> None:
    # Port 9 (discard) on loopback: nothing listens, the key fetch fails,
    # and an unverifiable receipt must not keep its claimed grade.
    genuine = _witness("http://127.0.0.1:9", WITNESS_PRIV, "mmr-verified")
    assert _receipt_grade(CHECKPOINT, genuine) is None


def test_receipt_grades_rejects_the_forged_signature_beside_a_genuine_one(
    witness_url: str, second_witness_url: str
) -> None:
    # Both URLs serve the witness key, so the forged entry reaches the
    # signature check and is rejected there, not by a failed key fetch.
    state = CheckpointWitnessState(
        entry_digest=CHECKPOINT.digest(),
        checkpoint=CHECKPOINT,
        effective_witnesses={
            witness_url: _witness(witness_url, WITNESS_PRIV, "mmr-verified"),
            second_witness_url: _witness(second_witness_url, ATTACKER_PRIV, "mmr-verified"),
        },
    )
    assert state.receipt_grades() == {witness_url: "mmr-verified", second_witness_url: None}
    assert state.receipt_grades(ts_pubkey_pem=WITNESS_PUB) == {
        witness_url: "mmr-verified",
        second_witness_url: None,
    }
