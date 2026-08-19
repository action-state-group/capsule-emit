# SPDX-License-Identifier: Apache-2.0
"""ECDSA signature malleability must not create a second entry identity.

An ECDSA signature is not a function of the act it signs: for any valid
``(r, s)``, ``(r, n-s)`` also verifies (SEC1 v2.0 SS4.1.3), and no private
key is needed to compute the twin from a public (payload, signature) pair.
``capsule_emit.bilateral.sig_digest`` binds later handshake phases to
earlier ones — see entry-identity-second-rule-sweep census — and previously
hashed the raw signature bytes, which are not stable per signing act.

This test builds a REAL malleated twin (flips s -> n-s on an actual ECDSA
P-256 signature, and confirms both encodings verify) and asserts sig_digest
is identical for both, plus a negative control proving the check is not a
constant function.
"""
from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from capsule_emit.bilateral import BilateralSig, request_payload, sig_digest

# NIST P-256 (secp256r1) group order (SEC2 SS2.4.2). Used to compute the
# malleated twin s' = n - s.
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _ecdsa_sign(key: ec.EllipticCurvePrivateKey, payload: bytes) -> tuple[bytes, bytes]:
    """Sign ``payload`` and return (original 64-byte r||s, malleated twin r||s).

    Both encodings are verified against the public key before being
    returned, so this is a real malleated twin, not a synthetic stand-in.
    """
    der = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    s_twin = _P256_N - s
    der_twin = encode_dss_signature(r, s_twin)

    pub = key.public_key()
    pub.verify(der, payload, ec.ECDSA(hashes.SHA256()))
    pub.verify(der_twin, payload, ec.ECDSA(hashes.SHA256()))
    assert s != s_twin, "malleation produced no change — test setup is broken"

    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_twin = r.to_bytes(32, "big") + s_twin.to_bytes(32, "big")
    assert sig != sig_twin
    return sig, sig_twin


def test_sig_digest_immune_to_signature_malleation():
    """A malleated twin of the same signing act must yield the same sig_digest."""
    key = ec.generate_private_key(ec.SECP256R1())
    payload = request_payload("org-a", "org-b", "a" * 64)

    sig_bytes, sig_bytes_twin = _ecdsa_sign(key, payload)

    original = BilateralSig(alg="ES256", key_id="org-a", signature=sig_bytes.hex())
    twin = BilateralSig(alg="ES256", key_id="org-a", signature=sig_bytes_twin.hex())

    assert sig_digest(original, payload) == sig_digest(twin, payload)


def test_sig_digest_negative_control_payload_change_still_differs():
    """A genuinely different act (1-bit payload change) MUST still produce a
    different digest — without this, the malleability-immunity assertion
    above would also pass for a constant function and prove nothing."""
    key = ec.generate_private_key(ec.SECP256R1())
    payload_a = request_payload("org-a", "org-b", "a" * 64)
    payload_b = request_payload("org-a", "org-b", "b" * 64)  # different action_digest

    sig_bytes, _ = _ecdsa_sign(key, payload_a)
    sig = BilateralSig(alg="ES256", key_id="org-a", signature=sig_bytes.hex())

    assert sig_digest(sig, payload_a) != sig_digest(sig, payload_b)
