# SPDX-License-Identifier: Apache-2.0
"""Signing identities, ``sign`` and ``seal``, mirroring Go ``sign.go``.

The Producer Envelope is a COSE_Sign1 over the raw 32-byte Capsule ID with
exactly these protected headers, in this order: content type, the raw
Ed25519 public key as ``kid``, then EdDSA ``alg``; the unprotected map is
empty. COSE encoding is ``scitt_cose``'s, as in :mod:`capsule_emit.signing`.
Ed25519 is deterministic, so one key and one Capsule give the same envelope
bytes in Go, TypeScript and Python.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scitt_cose.cose_sign1 import sign_sign1

from .build import _is_hex64, build
from .constants import CONTENT_TYPE
from .construction import build_composition
from .digest import digest_json
from .model import JSON_NULL, BuiltPayload, ComputeAttestation, Result, SealInput
from .verify import verify_capsule, verify_envelope

__all__ = [
    "SigningIdentity",
    "new_ed25519_signing_identity",
    "generate_signing_identity",
    "sign",
    "seal",
]

_SEED_SIZE = 32
_GO_PRIVATE_KEY_SIZE = 64


class SigningIdentity:
    """An immutable Ed25519 signer and its matching raw public key. Keeping
    the pair together prevents rotation between header construction and
    signature creation."""

    __slots__ = ("_private_key", "_public_key")

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("SigningIdentity needs an Ed25519PrivateKey")
        object.__setattr__(self, "_private_key", private_key)
        object.__setattr__(
            self,
            "_public_key",
            private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SigningIdentity is immutable")

    @property
    def public_key(self) -> bytes:
        """The raw 32-byte Ed25519 public key, carried as the envelope ``kid``."""
        return self._public_key

    def __repr__(self) -> str:
        return f"SigningIdentity(public_key={self._public_key.hex()})"

    def _private_key_pem(self) -> bytes:
        return self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def new_ed25519_signing_identity(private_key: bytes | Ed25519PrivateKey) -> SigningIdentity:
    """Construct a signing identity from an ``Ed25519PrivateKey``, a 32-byte
    seed, or Go's 64-byte private key form (seed followed by public key)."""
    if isinstance(private_key, Ed25519PrivateKey):
        return SigningIdentity(private_key)
    if not isinstance(private_key, (bytes, bytearray)):
        raise TypeError("Ed25519 private key must be bytes or an Ed25519PrivateKey")
    raw = bytes(private_key)
    if len(raw) == _GO_PRIVATE_KEY_SIZE:
        identity = SigningIdentity(Ed25519PrivateKey.from_private_bytes(raw[:_SEED_SIZE]))
        if identity.public_key != raw[_SEED_SIZE:]:
            raise ValueError("Ed25519 private key's public half does not match its seed")
        return identity
    if len(raw) != _SEED_SIZE:
        raise ValueError("Ed25519 private key has wrong size")
    return SigningIdentity(Ed25519PrivateKey.from_private_bytes(raw))


def generate_signing_identity() -> SigningIdentity:
    """Generate a fresh random Ed25519 signing identity."""
    return SigningIdentity(Ed25519PrivateKey.generate())


def sign(built: BuiltPayload, identity: SigningIdentity) -> bytes:
    """Create one Producer Envelope over an already-built Capsule's ID. The
    Capsule bytes are verified first and must match the ID."""
    if not isinstance(identity, SigningIdentity):
        raise ValueError("valid Ed25519 signing identity is required")
    if not isinstance(built, BuiltPayload):
        raise TypeError("sign takes a BuiltPayload")
    if not _is_hex64(built.capsule_id):
        raise ValueError("Capsule ID must be 64 lowercase hexadecimal characters")
    try:
        verified = verify_capsule(built.json)
    except (TypeError, ValueError):
        verified = None
    if verified is None or verified.capsule_id != built.capsule_id:
        raise ValueError("built Capsule does not match Capsule ID")
    protected = {3: CONTENT_TYPE, 4: identity.public_key}
    envelope = sign_sign1(
        bytes.fromhex(built.capsule_id),
        alg="EdDSA",
        private_key_pem=identity._private_key_pem(),
        protected=protected,
        unprotected={},
    )
    # The exact profile is agent-action-capsule's to check; a mismatch here is
    # a library defect, never a caller error.
    verify_envelope(built.capsule_id, envelope)
    return envelope


def seal(input: SealInput) -> Result:
    """Digest-commit caller JSON, build one signature-free Capsule (or
    composition) and attach one Producer Envelope. Writes nothing. Delegates
    to :func:`digest_json`, :func:`build` or :func:`build_composition`, and
    :func:`sign`."""
    if not isinstance(input, SealInput):
        raise TypeError("seal takes a SealInput")
    if input.capsule.model is not None or input.capsule.compute is not None:
        raise ValueError("SealInput model and compute metadata must use model and runtime fields")
    capsule = dataclasses.replace(input.capsule, model=input.model)
    if input.members is not None:
        if input.payload is not None or input.agent_output is not None:
            raise ValueError("composition seal must not include payload or agent output")
        if input.runtime:
            capsule = dataclasses.replace(capsule, compute=ComputeAttestation(runtime=input.runtime))
        built = build_composition(capsule, *input.members)
    else:
        agent_input = "" if input.payload is None else _digest("agent input", input.payload)
        agent_output = "" if input.agent_output is None else _digest("agent output", input.agent_output)
        if agent_input or agent_output or input.runtime:
            capsule = dataclasses.replace(
                capsule,
                compute=ComputeAttestation(
                    agent_input_digest=agent_input, agent_output_digest=agent_output, runtime=input.runtime
                ),
            )
        built = build(capsule)
    envelope = sign(built, input.identity)
    return Result(capsule_id=built.capsule_id, payload=bytes(built.json), envelope=envelope)


def _digest(what: str, value: Any) -> str:
    try:
        return digest_json(None if value is JSON_NULL else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"digest {what}: {exc}") from None
