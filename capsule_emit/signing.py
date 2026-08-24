# SPDX-License-Identifier: Apache-2.0
"""The producer :class:`Signer` seam — ``seal()``'s real signing surface.

Since 0.5.0 every capsule minted by ``seal()``/``carry()``/``compose()``
(``capsule_emit.core._emit_capsule``) carries a cryptographic signature over
its ``capsule_id`` plus the ``key_id`` that produced it -- this is the
*self-attested* rung of the ladder (frozen dev-surface v4 §2/§4): "your key,
your claim". It is what a lone producer has *before* any witness or anchor
ever sees the record, and it is what upgrades in place as checkpoints get
witnessed (see ``capsule_emit.witness``) -- a different, heavier layer that
signs MMR checkpoint digests, not capsule content, and is not this module.

**Signer protocol.** ``sign(payload: bytes) -> str`` (hex-encoded signature)
plus a stable ``key_id`` attribute. KMS/HSM/TPM signers are just other
implementations of this protocol -- ``capsule_emit`` never imports one
concretely, matching the frozen surface's "custody is pluggable at the one
seam custody flows through" (§7d).

**Default implementation.** :class:`LocalKeypairSigner` -- an Ed25519
keypair, auto-generated on first use and persisted to disk (PKCS8 PEM,
mode 0600) so the SAME key signs every capsule across process restarts, not
just within one process lifetime. ``key_id`` is the raw 32-byte public key,
hex-encoded -- a verifier needs nothing but the capsule itself to check the
signature (see :func:`verify_capsule_signature`); no key registry lookup.

**Persistence path.** One key per ledger by default -- ``<ledger>.signing_key.pem``
next to the ledger file, mirroring the per-ledger-path scoping
``capsule_emit.witness`` already uses for its own (unrelated) checkpoint
signer. Override with the ``signing_key_path=`` kwarg on ``seal()`` (threaded
through ``_emit_capsule``), or the ``CAPSULE_SIGNING_KEY_PATH`` env var, for a
single producer identity shared across ledgers.

**Rotation.** :meth:`LocalKeypairSigner.rotate` generates a new keypair and
returns a :class:`RotationRecord` binding old key to new: the OLD key signs
the NEW ``key_id``, so a party that already trusted the old key can verify
the succession without needing the old private key again (frozen surface
§7a: "the rotation record cites old key, new key, and the binding, so
identity survives rotation by construction"). Sealing that record as a
WHO-slot key-binding receipt is the caller's job -- this module only produces
the record; it does not seal one.
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "Signer",
    "LocalKeypairSigner",
    "RotationRecord",
    "SIGNING_KEY_PATH_ENV_VAR",
    "resolve_signer",
    "verify_capsule_signature",
]

#: Overrides the default per-ledger key path with one shared producer
#: identity. ``seal(..., signing_key_path=...)`` takes precedence over this.
SIGNING_KEY_PATH_ENV_VAR = "CAPSULE_SIGNING_KEY_PATH"


class Signer(Protocol):
    """``seal()``'s signing seam (frozen dev-surface v4 §7d). Any object with
    a stable ``key_id`` and a ``sign(payload: bytes) -> str`` method signing
    over arbitrary bytes and returning a hex-encoded signature."""

    key_id: str

    def sign(self, payload: bytes) -> str: ...


@dataclass(frozen=True)
class RotationRecord:
    """Binds a retired key to its successor -- the substrate for a §7a
    key-binding WHO-slot receipt. ``binding_signature`` is the OLD key's
    signature over the NEW ``key_id`` (ascii-encoded)."""

    old_key_id: str
    new_key_id: str
    binding_signature: str


def _pem_private_bytes(key) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _load_pem_private_key(data: bytes):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(data, password=None)


def _raw_public_hex(key) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


class LocalKeypairSigner:
    """Zero-config default producer signer: a persisted Ed25519 keypair.

    Auto-generates a fresh keypair the first time ``key_path`` does not
    exist, and persists it immediately so a second signer pointed at the
    same path -- in this process or a later one -- loads the identical key.
    ``key_id`` is the raw public key, hex-encoded (64 chars); a verifier
    reconstructs the public key directly from a capsule's ``key_id`` field
    -- see :func:`verify_capsule_signature`.
    """

    def __init__(self, key_path: str | os.PathLike) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._path = Path(key_path)
        self._lock = threading.Lock()
        if self._path.exists():
            self._private_key = _load_pem_private_key(self._path.read_bytes())
        else:
            self._private_key = Ed25519PrivateKey.generate()
            self._persist(self._private_key)
        self.key_id = _raw_public_hex(self._private_key)

    def _persist(self, key) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        pem = _pem_private_bytes(key)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def sign(self, payload: bytes) -> str:
        with self._lock:
            key = self._private_key
        return key.sign(payload).hex()

    def rotate(self, key_path: str | os.PathLike | None = None) -> RotationRecord:
        """Generate a new keypair, persist it (replacing the key at
        ``key_path``, or this signer's own path when omitted), and return
        the old-to-new :class:`RotationRecord`. After this call the signer
        signs with the NEW key."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        old_key_id = self.key_id
        old_private_key = self._private_key
        new_private_key = Ed25519PrivateKey.generate()
        new_key_id = _raw_public_hex(new_private_key)
        binding_signature = old_private_key.sign(new_key_id.encode("ascii")).hex()

        new_path = Path(key_path) if key_path is not None else self._path
        with self._lock:
            self._private_key = new_private_key
            self.key_id = new_key_id
            self._path = new_path
            self._persist(new_private_key)

        return RotationRecord(
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            binding_signature=binding_signature,
        )


def _default_key_path(ledger_path: str | os.PathLike) -> Path:
    return Path(os.fspath(ledger_path) + ".signing_key.pem")


_cache_lock = threading.Lock()
_cache: dict[str, LocalKeypairSigner] = {}


def resolve_signer(
    ledger_path: str | os.PathLike,
    *,
    signer: Signer | None = None,
    key_path: str | os.PathLike | None = None,
) -> Signer:
    """Resolve the :class:`Signer` a ``seal()`` call actually signs with.

    Precedence: an explicit ``signer=`` object wins outright (bring your own
    KMS/HSM); else an explicit ``key_path=``; else ``CAPSULE_SIGNING_KEY_PATH``;
    else a key file next to ``ledger_path``. The resolved
    :class:`LocalKeypairSigner` is cached per resolved key path so repeated
    ``seal()`` calls against the same ledger reuse one signer object instead
    of re-reading the key file every time.
    """
    if signer is not None:
        return signer

    resolved_path = Path(
        key_path
        if key_path is not None
        else os.environ.get(SIGNING_KEY_PATH_ENV_VAR) or _default_key_path(ledger_path)
    ).resolve()
    cache_key = str(resolved_path)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is None:
            cached = LocalKeypairSigner(resolved_path)
            _cache[cache_key] = cached
        return cached


def verify_capsule_signature(capsule: dict) -> bool:
    """Recompute and check a capsule's self-attested signature. Never raises.

    Self-contained: reconstructs the Ed25519 public key straight from the
    capsule's own ``key_id`` field (the raw public key, hex-encoded) -- no
    key registry lookup needed. This proves "the holder of this key signed
    this exact content"; it does NOT prove who that key belongs to (see the
    frozen dev-surface v4 §7a identity-binding layers for that).

    What got signed is NOT ``capsule_id`` itself -- ``capsule_id`` is
    computed AFTER ``signature``/``key_id`` are added (see
    ``capsule_emit.core._emit_capsule``), so it commits to them too. What
    was signed is the content digest *without* ``signature``/``key_id``
    present, which this function reconstructs the same way: strip both
    fields and recompute via ``compute_capsule_id`` (which already excludes
    ``capsule_id``/``chain``).
    """
    from agent_action_capsule.canonical import compute_capsule_id
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        signature_hex = capsule["signature"]
        key_id = capsule["key_id"]
        core = {k: v for k, v in capsule.items() if k not in ("signature", "key_id")}
        content_digest = compute_capsule_id(core)
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_id))
        public_key.verify(bytes.fromhex(signature_hex), content_digest.encode("ascii"))
        return True
    except (KeyError, ValueError, InvalidSignature, TypeError):
        return False
