# SPDX-License-Identifier: Apache-2.0
"""The producer :class:`Signer` seam — ``seal()``'s real signing surface.

Since 0.5.0 every capsule minted by ``seal()``/``received()`` (standalone or
composed via a slot wrapper -- ``capsule_emit.core._emit_capsule``) carries a
cryptographic proof over its ``capsule_id`` plus the ``key_id`` that
produced it -- this is the
*self-attested* rung of the ladder (frozen dev-surface v4 §2/§4): "your key,
your claim". It is what a lone producer has *before* any witness or anchor
ever sees the record, and it is what upgrades in place as checkpoints get
witnessed (see ``capsule_emit.witness``) -- a different, heavier layer that
signs MMR checkpoint digests, not capsule content, and is not this module.

**draft-04 reversal ([capsule-cose-sign1], 2026-08-24).** The producer proof
is a **COSE_Sign1 envelope** over the raw 32-byte ``capsule_id`` digest (the
frozen AAC producer-envelope profile: ``alg=EdDSA``, ``content_type``
``application/agent-action-capsule-id``, ``kid`` = the raw 32-byte Ed25519
public key, empty unprotected map) -- reusing ``scitt_cose.cose_sign1`` for
the COSE/CBOR machinery (boundary rule: no hand-rolled COSE). ``capsule_id``
itself is signer-independent again: computed over the signature-free payload
(see ``capsule_emit.canonicalization``), the same for any signer over
identical content, exactly as pre-#94. ``capsule["signature"]`` now carries
the hex-encoded COSE_Sign1 envelope (previously a bare 64-byte Ed25519
signature); ``capsule["key_id"]`` is unchanged (the raw public key, hex).
Both fields are added to the capsule dict *after* ``capsule_id`` is computed
and are never part of its preimage (see ``capsule_emit.canonicalization``'s
``_LOCAL_ONLY_FIELDS``) -- no fold-in, no exclusion-hack recompute dance.

**Signer protocol.** ``sign(payload: bytes) -> (signature, key_id)`` -- a
single atomic call returning a hex-encoded signature paired with the
hex-encoded id of the key that produced it. This is the frozen §7d shape
verbatim, and it is atomic on purpose: an earlier draft split this into
``sign(payload) -> str`` plus a separately-read mutable ``key_id``
attribute, which let a ``rotate()`` land between the two reads and mint a
capsule signed by the OLD key but labeled with the NEW ``key_id``. Returning
both from one call makes that pairing correct by construction -- there is no
window between "which key signed" and "which key_id got recorded" for a
concurrent rotation to land in. KMS/HSM/TPM signers are just other
implementations of this protocol -- ``capsule_emit`` never imports one
concretely, matching the frozen surface's "custody is pluggable at the one
seam custody flows through" (§7d). This generic ``sign()`` stays the
protocol's one REQUIRED method, used verbatim for producer signing by any
``Signer`` that does not additionally implement the OPTIONAL
``sign_envelope(payload: bytes) -> (envelope, key_id)`` capability below --
its output is stored as-is (a documented escape hatch for a bring-your-own
signer that predates this profile, or chooses not to implement it); it
simply will not verify as a conformant COSE_Sign1 envelope, which
:func:`verify_capsule_signature` reports honestly (``False``), same as any
other malformed/foreign signature.

**Default implementation.** :class:`LocalKeypairSigner` -- an Ed25519
keypair, auto-generated on first use and persisted to disk (PKCS8 PEM,
mode 0600) so the SAME key signs every capsule across process restarts, not
just within one process lifetime. ``key_id`` is the raw 32-byte public key,
hex-encoded -- a verifier needs nothing but the capsule itself to check the
signature (see :func:`verify_capsule_signature`); no key registry lookup.
It implements ``sign()`` (a bare Ed25519 signature -- still used as-is by
the unrelated checkpoint-signing path, see ``capsule_emit.witness``'s
``_PersistedCheckpointSigner``, and by ``rotate()``'s key-binding receipt),
``sign_envelope()`` (a real COSE_Sign1 producer envelope, used for producer
signing -- see :func:`sign_producer_envelope`), AND
``sign_cose_statement()`` (a generic SCITT Signed Statement with caller-
supplied content type and CWT ``iss``/``sub`` claims -- used by
``capsule_emit.checkpoint.cose_wire`` to put a CLL checkpoint on the wire,
[cll-checkpoint-cose-wire]).

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

import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

__all__ = [
    "Signer",
    "LocalKeypairSigner",
    "RotationRecord",
    "SIGNING_KEY_PATH_ENV_VAR",
    "resolve_signer",
    "sign_producer_envelope",
    "AuthorshipVerdict",
    "verify_capsule_signature",
    "verify_capsule_signature_tristate",
    "verify_store_signed",
]

#: Overrides the default per-ledger key path with one shared producer
#: identity. ``seal(..., signing_key_path=...)`` takes precedence over this.
SIGNING_KEY_PATH_ENV_VAR = "CAPSULE_SIGNING_KEY_PATH"


class Signer(Protocol):
    """``seal()``'s signing seam (frozen dev-surface v4 §7d). Any object with
    a ``sign(payload: bytes) -> (signature, key_id)`` method: signs arbitrary
    bytes and atomically returns the hex-encoded signature together with the
    hex-encoded id of the key that produced it, so a caller never reads
    ``key_id`` as a step separate from the signature it labels.

    A signer MAY additionally implement ``sign_envelope(payload: bytes) ->
    (envelope, key_id)`` -- same atomic-pair contract, but ``envelope`` is a
    hex-encoded COSE_Sign1 producer envelope over ``payload`` rather than a
    bare signature (see :func:`sign_producer_envelope`, which duck-types this
    capability and falls back to ``sign()`` when absent).
    """

    def sign(self, payload: bytes) -> tuple[str, str]: ...


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

    def sign(self, payload: bytes) -> tuple[str, str]:
        """Sign ``payload`` and return ``(signature, key_id)`` as one atomic
        pair, both read under the same lock ``rotate()`` swaps them under --
        so a rotation landing concurrently can never pair a signature from
        one key with the ``key_id`` of another.

        Returns a bare 64-byte Ed25519 signature (hex) -- used as-is by
        callers that need a raw signature over arbitrary bytes: the
        unrelated checkpoint-signing path (``capsule_emit.witness``'s
        ``_PersistedCheckpointSigner``) and ``rotate()``'s key-binding
        receipt. Producer capsule signing uses :meth:`sign_envelope`
        instead -- see :func:`sign_producer_envelope`.
        """
        with self._lock:
            key = self._private_key
            key_id = self.key_id
        return key.sign(payload).hex(), key_id

    def sign_envelope(self, payload: bytes) -> tuple[str, str]:
        """Build a COSE_Sign1 producer envelope over ``payload`` (the raw
        32-byte ``capsule_id`` digest -- the frozen AAC producer-envelope
        profile) and return ``(envelope_hex, key_id)`` as one atomic pair,
        for the same reason :meth:`sign` does: both are read from the SAME
        lock-protected snapshot of this signer's key, so a concurrent
        ``rotate()`` can never pair an envelope signed by one key with
        another key's ``key_id``.

        Reuses ``scitt_cose.cose_sign1.sign_sign1`` for the COSE/CBOR
        machinery (boundary rule: no hand-rolled COSE) -- this signer has
        direct access to its own private key, unlike an arbitrary
        bytes-in/bytes-out :class:`Signer`, so it can build a real,
        cross-verifiable envelope in one atomic step. See
        :func:`sign_producer_envelope` for the generic-signer fallback.
        """
        from agent_action_capsule.media_types import CAPSULE_ID_MEDIA_TYPE
        from scitt_cose.cose_sign1 import sign_sign1

        with self._lock:
            key = self._private_key
            key_id = self.key_id
        protected = {3: CAPSULE_ID_MEDIA_TYPE, 4: bytes.fromhex(key_id)}
        envelope = sign_sign1(
            payload,
            alg="EdDSA",
            private_key_pem=_pem_private_bytes(key),
            protected=protected,
            unprotected={},
        )
        return envelope.hex(), key_id

    def sign_cose_statement(
        self,
        payload: bytes,
        *,
        content_type: str,
        issuer: str,
        subject: str,
        extra_cwt_claims: dict | None = None,
    ) -> bytes:
        """Build a generic SCITT Signed Statement (COSE_Sign1) over
        ``payload``, with this signer's key as ``kid`` and CWT ``iss``/
        ``sub`` identity claims in the protected header -- the profile
        ``capsule_emit.checkpoint.cose_wire`` uses to put a CLL checkpoint on
        the wire ([cll-checkpoint-cose-wire]), and any future caller wanting
        the same signed-statement shape over a different payload/claims.

        Reuses ``scitt_cose.statement.build_signed_statement`` for the
        COSE/CBOR machinery (boundary rule: no hand-rolled COSE) --
        unlike :meth:`sign_envelope` (the fixed AAC producer-envelope
        profile: no CWT claims, a pinned content type), this is the
        general-purpose sibling: caller-supplied content type and CWT
        identity. Both share the same atomicity contract as :meth:`sign`/
        :meth:`sign_envelope`: the key and ``key_id`` are read together
        under one lock so a concurrent ``rotate()`` can never pair a
        statement signed by one key with another key's ``kid``.
        """
        from scitt_cose.statement import build_signed_statement

        with self._lock:
            key = self._private_key
            key_id = self.key_id
        return build_signed_statement(
            payload,
            alg="EdDSA",
            private_key_pem=_pem_private_bytes(key),
            issuer=issuer,
            subject=subject,
            content_type=content_type,
            extra_cwt_claims=extra_cwt_claims,
            kid=bytes.fromhex(key_id),
        )

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


def sign_producer_envelope(signer: Signer, capsule_id: str) -> tuple[str, str]:
    """Sign ``capsule_id`` (lowercase hex) and return ``(envelope_hex,
    key_id)`` -- the frozen AAC producer-envelope profile: a COSE_Sign1
    envelope over the raw 32-byte digest (decoded from the lowercase-hex id,
    NOT its ASCII bytes).

    Signers implementing the optional ``sign_envelope`` capability (e.g.
    :class:`LocalKeypairSigner`) produce a real, cross-verifiable COSE_Sign1
    envelope. Other :class:`Signer` implementations fall back to the generic
    ``sign()`` contract verbatim -- see the module docstring's "Signer
    protocol" section for why that is a documented escape hatch, not a bug.
    """
    payload = bytes.fromhex(capsule_id)
    sign_envelope = getattr(signer, "sign_envelope", None)
    if callable(sign_envelope):
        return sign_envelope(payload)
    return signer.sign(payload)


class AuthorshipVerdict(str, Enum):
    """Three-state per-entry authorship verdict
    [verify-entry-authorship-tristate-and-log] -- the SAME shape already
    shipped for witness-stamp authenticity (see
    ``cll.checkpoint.emit.StampVerdict``, which folds in the self-hosted-TS
    pin case too): a claim, once made, is either upheld or a forgery: never
    is *absence* of a claim treated as forgery. Capsule-authorship and
    witness-stamp authenticity are different concepts living in different
    repos (``capsule_emit`` carries capsule vocabulary that
    ``cll``/``cll.checkpoint`` deliberately never does -- see
    ``capsule_emit.bundle``'s module docstring), so this is a distinct enum
    rather than a re-exported one -- "one shared tri-state code path" here
    means every capsule-emit call site that must grade authorship
    (:func:`capsule_emit.bundle.verify_bundle`,
    :func:`verify_store_signed`) routes through the ONE function below
    (:func:`verify_capsule_signature_tristate`), not that the two repos
    share a Python class across the capsule/log boundary.
    """

    AUTHORED = "authored"
    """Claimed (``signature``+``key_id`` present) and the producer envelope
    verifies. The full self-attested authorship guarantee."""

    UNCLAIMED = "unclaimed"
    """No producer signature was ever claimed (``signature``/``key_id`` both
    absent -- e.g. a :func:`capsule_emit.surface.log` entry). NOT evidence of
    forgery -- the log-integrity guarantees (order, completeness,
    contemporaneity, tamper-evidence, witnessing) still hold from the CLL
    layer alone; only per-record *authorship* is unclaimed."""

    INVALID = "invalid"
    """Either a malformed claim (only one of ``signature``/``key_id``
    present) or a claim that was made and fails to verify -- forgery, or
    content/signature/key_id tampered after signing."""


def verify_capsule_signature_tristate(capsule: dict) -> tuple[AuthorshipVerdict, list[str]]:
    """Grade a capsule's self-attested producer-authorship claim as one of
    THREE states -- never raises. See :class:`AuthorshipVerdict` for the
    discipline this enforces: absence of a claim (``signature``/``key_id``
    both missing, e.g. a :func:`capsule_emit.surface.log` entry) is
    :attr:`AuthorshipVerdict.UNCLAIMED`, never :attr:`AuthorshipVerdict.INVALID`
    -- conflating the two would report a genuinely-unsigned, honestly-logged
    entry as forged.

    ``capsule["signature"]`` is a hex-encoded COSE_Sign1 envelope over the
    raw ``capsule_id`` digest (the frozen AAC producer-envelope profile);
    verification reuses ``agent_action_capsule.producer_envelope`` (which in
    turn reuses ``scitt_cose`` for the COSE/CBOR machinery -- boundary rule:
    no hand-rolled COSE). This proves "the holder of this key signed this
    exact ``capsule_id``"; it does NOT prove who that key belongs to (see the
    frozen dev-surface v4 §7a identity-binding layers for that), and it does
    NOT by itself prove ``capsule_id`` matches this capsule's carried value
    -- callers that read a carried ``capsule_id`` separately (e.g.
    ``capsule_emit.bundle.verify_bundle``) check that independently.

    ``capsule_id`` is signer-independent (draft-04 reversal,
    [capsule-cose-sign1]): computed over the signature-free payload,
    excluding only ``capsule_id`` itself plus ``signature``/``key_id`` (see
    ``capsule_emit.canonicalization`` -- never folded in, so no strip-and-
    recompute dance is needed here; ``compute_capsule_id`` already excludes
    them, which is exactly what makes an absent-signature entry's
    ``capsule_id`` identical to what it would be once later signed).

    Returns ``(verdict, messages)`` -- ``messages`` is empty for
    :attr:`AuthorshipVerdict.AUTHORED` and :attr:`AuthorshipVerdict.UNCLAIMED`
    (nothing wrong to report), and carries a human-readable finding for
    :attr:`AuthorshipVerdict.INVALID`.
    """
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    from .canonicalization import compute_capsule_id

    if not isinstance(capsule, dict):
        return AuthorshipVerdict.INVALID, ["not a mapping"]

    cid = capsule.get("capsule_id", "<none>")
    has_signature = "signature" in capsule
    has_key_id = "key_id" in capsule

    if not has_signature and not has_key_id:
        return AuthorshipVerdict.UNCLAIMED, []
    if has_signature != has_key_id:
        return AuthorshipVerdict.INVALID, [
            f"capsule_id={cid}: only one of signature/key_id present -- "
            "malformed producer envelope"
        ]

    try:
        envelope_hex = capsule["signature"]
        key_id = capsule["key_id"]
        capsule_id = compute_capsule_id(capsule)
        envelope = bytes.fromhex(envelope_hex)
        result = verify_producer_envelope(capsule_id, envelope)
        ok = result.ok and result.public_key == bytes.fromhex(key_id)
    except (KeyError, ValueError, TypeError) as exc:
        return AuthorshipVerdict.INVALID, [f"capsule_id={cid}: producer envelope malformed: {exc}"]

    if ok:
        return AuthorshipVerdict.AUTHORED, []
    return AuthorshipVerdict.INVALID, [
        f"capsule_id={cid}: self-attested Ed25519 signature does not verify against key_id -- "
        "content, signature, or key_id was tampered or forged"
    ]


def verify_capsule_signature(capsule: dict) -> bool:
    """Thin bool collapse of :func:`verify_capsule_signature_tristate` --
    ``True`` iff :attr:`AuthorshipVerdict.AUTHORED`. Every capsule minted by
    ``seal()``/``received()`` is always signed (0.5.0, no opt-out), so for
    that whole population this is equivalent to the pre-tristate behavior.
    Callers that must tell an honestly-unclaimed :func:`capsule_emit.surface.log`
    entry apart from a forged one -- :func:`capsule_emit.bundle.verify_bundle`,
    :func:`verify_store_signed` -- use the tristate function instead; this
    bool form flattens :attr:`AuthorshipVerdict.UNCLAIMED` and
    :attr:`AuthorshipVerdict.INVALID` together, which is exactly the
    conflation [verify-entry-authorship-tristate-and-log] fixes at those two
    call sites.
    """
    verdict, _ = verify_capsule_signature_tristate(capsule)
    return verdict is AuthorshipVerdict.AUTHORED


def verify_store_signed(records: list[dict]) -> list:
    """``agent_action_capsule.verify_store(records)``, plus the producer
    signature check that verifier deliberately never performs.

    ``agent_action_capsule.verify`` is the neutral Class 1 payload verifier
    (spec §6): its own docstring excludes "the COSE_Sign1 signature ...
    by reference" as substrate-verifier territory. ``capsule_emit``'s
    self-attested producer envelope (this module) is exactly that substrate
    layer for the self-attested rung -- it is a ``capsule_emit`` concept, not
    part of the neutral spec, so it does not belong inside
    ``agent_action_capsule`` itself. Every ``capsule-emit`` surface that
    renders a verify verdict (CLI ``verify``, ``ledger view``'s inline verify
    column, ``permalink``'s ``check_capsules``) must compose the two checks
    here rather than trust ``verify_store`` alone: ``verify_store`` only
    confirms ``capsule_id`` recomputes from the carried content -- it never
    checks that any cryptographic proof exists over it at all, so a wholly
    attacker-authored capsule (fabricated content, a self-consistent
    ``capsule_id``, no real signer behind it) reports VALID from that check
    alone. Only :func:`verify_capsule_signature` confirms a COSE_Sign1
    envelope genuinely verifies against ``capsule_id`` under the key named
    in ``key_id``.

    Mutates and returns the same ``VerificationResult`` list
    ``verify_store`` produces (``result.ok``/``result.findings`` gain the
    producer-authorship verdict) so every existing caller of ``verify_store``
    becomes a drop-in caller of this instead. Never raises.

    **Three-state, not two** [verify-entry-authorship-tristate-and-log]: an
    entry with no producer signature at all (e.g. a
    :func:`capsule_emit.surface.log` entry) gets a ``severity="warning"``
    finding (``producer_signature_unclaimed``) that does NOT gate
    ``result.ok`` -- ``agent_action_capsule.contracts``'s own severity
    convention already treats ``"warning"`` as non-gating (see
    ``Finding``'s docstring), so this reuses that convention rather than
    inventing a new one. Only a claimed-and-failing signature
    (``producer_signature_invalid``, ``severity="error"``) gates ``ok``, the
    same as before.
    """
    from agent_action_capsule import Finding

    from .verification import verify_store

    results = verify_store(records)
    for record, result in zip(records, results):
        verdict, messages = verify_capsule_signature_tristate(record)
        cid = record.get("capsule_id", "<none>") if isinstance(record, dict) else "<none>"
        if verdict is AuthorshipVerdict.INVALID:
            result.findings.append(
                Finding(
                    code="producer_signature_invalid",
                    detail="; ".join(messages) or f"capsule_id={cid}: producer signature invalid",
                    severity="error",
                )
            )
        elif verdict is AuthorshipVerdict.UNCLAIMED:
            result.findings.append(
                Finding(
                    code="producer_signature_unclaimed",
                    detail=(
                        f"capsule_id={cid}: no producer signature present -- "
                        "log-verified, authorship not claimed"
                    ),
                    severity="warning",
                )
            )
        result.ok = not any(f.severity == "error" for f in result.findings)
    return results
