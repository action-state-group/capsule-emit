# SPDX-License-Identifier: Apache-2.0
"""Shared helper: mint a REAL, verifiable COSE Receipt for stub-TS test
doubles ([stamp-authenticity-on-read-not-presence]).

Before this fix, every stub Transparency Service in this test suite
returned literal garbage for ``receipt_b64``
(``base64.b64encode(b"stub-receipt-not-a-real-cose-receipt")``) — which made
every "successfully witnessed" checkpoint built in tests byte-for-byte
indistinguishable from a file-forger's fabricated stamp. Once
``grade()``/``verify_bundle`` actually check stamp authenticity, tests that
exercise a genuinely-witnessed checkpoint need their stub TS to mint a real,
structurally valid COSE_Sign1 Receipt (RFC 9162 SHA-256, via
``scitt_cose.build_receipt``) — this module is that, using a fixed
per-process test-only Ed25519 keypair so tests can also exercise the
stronger pubkey-pinned verification path.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from scitt_cose import build_receipt

_TEST_TS_PRIVATE_KEY = Ed25519PrivateKey.generate()

#: PEM of the fixed test TS keypair -- for tests that want the full
#: pubkey-pinned ``verify_witness_stamp_offline(..., ts_pubkey_pem=...)`` path.
TEST_TS_PRIVATE_KEY_PEM = _TEST_TS_PRIVATE_KEY.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
)
TEST_TS_PUBLIC_KEY_PEM = _TEST_TS_PRIVATE_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)


def build_stub_receipt_b64(entry_hash: str) -> str:
    """Mint a real, single-leaf COSE Receipt over ``entry_hash`` with the
    fixed test TS key, base64-encoded -- a drop-in replacement for the old
    garbage stub bytes in a stub-TS HTTP handler's ``/v1/digest`` response."""
    receipt_bytes = build_receipt(
        leaf_entry_hex=entry_hash,
        leaf_index=0,
        tree_entries_hex=[entry_hash],
        alg="EdDSA",
        log_private_key_pem=TEST_TS_PRIVATE_KEY_PEM,
    )
    return base64.b64encode(receipt_bytes).decode()
