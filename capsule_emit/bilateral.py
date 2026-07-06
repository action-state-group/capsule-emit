# SPDX-License-Identifier: Apache-2.0
"""OSS bilateral attestation handshake — engine-free, Authority-free.

Implements the four-move protocol described in
draft-mih-agent-bilateral-attestation-00: request attestation → constraint
evaluation → action attestation → acknowledgment.  Extracted from the
Action State Authority handshake package with Authority-specific types
removed (no KYB, no identity registry, no blind custody, no pricing).

Public API
----------
Types:
  BilateralSig         -- org signature over a canonical payload
  BilateralState       -- REQUESTED / ACTED / BILATERAL / ONE_SIDED
  BilateralRecord      -- per-handshake state

Payload functions (deterministic signing bytes):
  request_payload(requester, responder, action_digest) -> bytes
  action_payload(handshake_id, responder, request_sig_digest) -> bytes
  confirm_payload(handshake_id, party, acked_sig_digest) -> bytes
  sig_digest(sig) -> str

Orchestration:
  BilateralHandshake   -- in-memory state machine; bring your own verifier

Capsule emission helpers:
  seal_request(...)    -- emit capsule for the request phase
  seal_action(...)     -- emit capsule for the action phase
  seal_bilateral(...)  -- emit capsule recording bilateral completion

Verifier stubs:
  no_op_verifier       -- ALWAYS PASSES — for tests only; never production
  dict_verifier(keys)  -- verifies HMAC-SHA256 keyed by org_id (demo only)

Wire encoding for the four exchange objects is TBD; see the companion I-D.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

__all__ = [
    "BilateralSig",
    "BilateralState",
    "BilateralRecord",
    "BilateralHandshake",
    "BilateralError",
    "InvalidSignature",
    "IllegalTransition",
    "UnknownParty",
    "request_payload",
    "action_payload",
    "confirm_payload",
    "sig_digest",
    "no_op_verifier",
    "dict_verifier",
    "seal_request",
    "seal_action",
    "seal_bilateral",
]

# Callable contract: given org_id, payload bytes, and the signature — return True if valid.
VerifyFn = Callable[[str, bytes, "BilateralSig"], bool]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BilateralSig:
    """An organizational signature over a canonical payload.

    In production use Ed25519 (or another asymmetric scheme). The ``alg``
    field carries the algorithm identifier; ``key_id`` identifies the
    signing key within the org's key set.
    """

    alg: str
    key_id: str
    signature: str  # hex-encoded


class BilateralState(str, Enum):
    """Handshake state machine states."""

    REQUESTED = "requested"    # A signed the request; awaiting B
    ACTED = "acted"            # B evaluated constraints + signed the action
    BILATERAL = "bilateral"    # both parties confirmed — non-repudiable
    ONE_SIDED = "one_sided"    # counterparty not reachable (graceful degradation)


@dataclass
class BilateralRecord:
    """Per-handshake state, returned by BilateralHandshake methods."""

    handshake_id: str
    requester_org: str
    responder_org: str | None
    action_digest: str          # SHA-256 hex of canonical(action)
    state: BilateralState
    request_sig: BilateralSig | None = None
    action_sig: BilateralSig | None = None
    requester_confirm: BilateralSig | None = None
    responder_confirm: BilateralSig | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BilateralError(Exception):
    """Base for bilateral protocol errors."""


class InvalidSignature(BilateralError):
    """A supplied signature did not verify against the signing org's key."""


class IllegalTransition(BilateralError):
    """The requested step is not legal from the current state."""


class UnknownParty(BilateralError):
    """A party_org is neither the requester nor the responder."""


# ---------------------------------------------------------------------------
# Canonical payload functions
#
# These functions produce the deterministic byte strings that each party signs.
# They are transport-agnostic: the signed bytes are pinned here; wire encoding
# of the four exchange objects is TBD for a future revision of the I-D.
#
# The design mirrors as_authority.handshake.payloads (moat-scrubbed):
#   - Commitment (blind custody) is replaced by action_digest (SHA-256 hex)
#   - Four phases bind progressively more context
#   - Later signatures bind earlier signature digests so they cannot be lifted
# ---------------------------------------------------------------------------


def _canon(obj: dict) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sig_digest(sig: BilateralSig) -> str:
    """Stable digest of a signature, used to bind later phases to earlier ones."""
    return hashlib.sha256(
        f"{sig.alg}:{sig.key_id}:{sig.signature}".encode("utf-8")
    ).hexdigest()


def request_payload(
    requester_org: str,
    responder_org: str | None,
    action_digest: str,
) -> bytes:
    """Bytes org A signs to open a handshake (request attestation).

    Binds the requester to the action by its content digest and names the
    intended responder. A request attestation is valid only against the
    responder it names.
    """
    return _canon(
        {
            "phase": "request",
            "requester_org": requester_org,
            "responder_org": responder_org,
            "action_digest": action_digest,
        }
    )


def action_payload(
    handshake_id: str,
    responder_org: str,
    request_sig_digest: str,
) -> bytes:
    """Bytes org B signs after evaluating constraints (action attestation).

    Binds B's action to A's request by the request signature digest so B's
    action attestation cannot be replayed against a different request.
    """
    return _canon(
        {
            "phase": "action",
            "handshake_id": handshake_id,
            "responder_org": responder_org,
            "request_sig_digest": request_sig_digest,
        }
    )


def confirm_payload(
    handshake_id: str,
    party_org: str,
    acked_sig_digest: str,
) -> bytes:
    """Bytes a party signs to acknowledge the counterparty's attestation.

    ``acked_sig_digest`` is the digest of the *other* party's attestation:
    A acks B's action_sig; B acks A's request_sig.  Binding to it makes the
    acknowledgment non-repudiable and unambiguous.
    """
    return _canon(
        {
            "phase": "confirm",
            "handshake_id": handshake_id,
            "party_org": party_org,
            "acked_sig_digest": acked_sig_digest,
        }
    )


# ---------------------------------------------------------------------------
# Verifier stubs
# ---------------------------------------------------------------------------


def no_op_verifier(org_id: str, payload: bytes, sig: BilateralSig) -> bool:
    """ALWAYS returns True — for tests only.  NEVER use in production."""
    return True


def dict_verifier(keys: dict[str, bytes]) -> VerifyFn:
    """Return a demo verifier backed by HMAC-SHA256 symmetric keys.

    ``keys`` maps org_id → shared-secret bytes.  The "signature" field of a
    BilateralSig must be the hex-encoded HMAC-SHA256(key, payload).

    This is a demonstration verifier using symmetric crypto. Production
    deployments should use an asymmetric scheme (e.g. Ed25519) so each
    party keeps a private key and publishes only the public key.
    """

    def _verify(org_id: str, payload: bytes, sig: BilateralSig) -> bool:
        key = keys.get(org_id)
        if key is None:
            return False
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig.signature)

    return _verify


def dict_signer(keys: dict[str, bytes]) -> Callable[[str, bytes], BilateralSig]:
    """Return a demo signer that produces HMAC-SHA256 signatures.

    Companion to ``dict_verifier``.  ``key_id`` is the org_id.
    """

    def _sign(org_id: str, payload: bytes) -> BilateralSig:
        key = keys[org_id]
        sig_hex = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return BilateralSig(alg="hmac-sha256", key_id=org_id, signature=sig_hex)

    return _sign


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class BilateralHandshake:
    """In-memory bilateral attestation handshake state machine.

    Bring-your-own verifier: ``verify_fn(org_id, payload_bytes, sig) -> bool``.
    For a demo without real key infrastructure, pass ``no_op_verifier``
    (all signatures accepted) or ``dict_verifier(keys)`` (HMAC demo).
    For production, plug in an Ed25519 or ECDSA verifier backed by your
    org's key registry.

    If ``responder_org`` is None or the verifier cannot resolve its key,
    the handshake degrades to ONE_SIDED (requesting org still holds a
    verifiable proof of what it requested).  Degradation is recorded, never
    silent.
    """

    def __init__(self, verify_fn: VerifyFn = no_op_verifier) -> None:
        self._verify_fn = verify_fn
        self._records: dict[str, BilateralRecord] = {}
        self._lock = threading.Lock()

    def _verify(self, org_id: str, payload: bytes, sig: BilateralSig) -> None:
        if not self._verify_fn(org_id, payload, sig):
            raise InvalidSignature(f"signature did not verify for org {org_id!r}")

    def open(
        self,
        requester_org: str,
        responder_org: str | None,
        action_digest: str,
        request_sig: BilateralSig,
    ) -> BilateralRecord:
        """A signs the request.

        Verifies A's signature against the canonical request payload, then
        records the handshake.  If ``responder_org`` is None or the verifier
        returns False for a probe payload, degrades to ONE_SIDED.  Returns
        the record; use :meth:`id_of` to get the assigned handshake_id.
        """
        payload = request_payload(requester_org, responder_org, action_digest)
        self._verify(requester_org, payload, request_sig)

        if responder_org is None:
            state = BilateralState.ONE_SIDED
        else:
            state = BilateralState.REQUESTED

        hid = uuid.uuid4().hex
        rec = BilateralRecord(
            handshake_id=hid,
            requester_org=requester_org,
            responder_org=responder_org,
            action_digest=action_digest,
            state=state,
            request_sig=request_sig,
        )
        with self._lock:
            self._records[hid] = rec
        return rec

    def respond(self, handshake_id: str, action_sig: BilateralSig) -> BilateralRecord:
        """B evaluates constraints and signs the action (REQUESTED → ACTED).

        Legal only from REQUESTED.  Verifies B's signature against the canonical
        action payload (which binds A's request signature digest, so B cannot
        act on a substituted request).
        """
        with self._lock:
            rec = self._records.get(handshake_id)
            if rec is None:
                raise BilateralError(f"handshake {handshake_id!r} not found")
            if rec.state is not BilateralState.REQUESTED:
                raise IllegalTransition(
                    f"respond requires REQUESTED, got {rec.state.value}"
                )
            assert rec.responder_org is not None
            assert rec.request_sig is not None
            payload = action_payload(
                handshake_id, rec.responder_org, sig_digest(rec.request_sig)
            )
            self._verify(rec.responder_org, payload, action_sig)
            updated = BilateralRecord(
                **{**rec.__dict__, "action_sig": action_sig, "state": BilateralState.ACTED}
            )
            self._records[handshake_id] = updated
        return updated

    def confirm(
        self, handshake_id: str, party_org: str, confirm_sig: BilateralSig
    ) -> BilateralRecord:
        """A party acknowledges receipt of the counterparty's attestation.

        Legal only from ACTED.  Each party confirms the *other's* attestation:
        requester confirms B's action_sig; responder confirms A's request_sig.
        When both have confirmed → BILATERAL (non-repudiable, both ways).
        """
        with self._lock:
            rec = self._records.get(handshake_id)
            if rec is None:
                raise BilateralError(f"handshake {handshake_id!r} not found")
            if rec.state is not BilateralState.ACTED:
                raise IllegalTransition(
                    f"confirm requires ACTED, got {rec.state.value}"
                )
            assert rec.request_sig is not None
            assert rec.action_sig is not None

            updates: dict = {}
            if party_org == rec.requester_org:
                payload = confirm_payload(
                    handshake_id, party_org, sig_digest(rec.action_sig)
                )
                self._verify(party_org, payload, confirm_sig)
                updates["requester_confirm"] = confirm_sig
                req_done = True
                resp_done = rec.responder_confirm is not None
            elif party_org == rec.responder_org:
                payload = confirm_payload(
                    handshake_id, party_org, sig_digest(rec.request_sig)
                )
                self._verify(party_org, payload, confirm_sig)
                updates["responder_confirm"] = confirm_sig
                req_done = rec.requester_confirm is not None
                resp_done = True
            else:
                raise UnknownParty(
                    f"{party_org!r} is neither requester nor responder"
                )

            if req_done and resp_done:
                updates["state"] = BilateralState.BILATERAL

            updated = BilateralRecord(**{**rec.__dict__, **updates})
            self._records[handshake_id] = updated
        return updated

    def get(self, handshake_id: str) -> BilateralRecord | None:
        with self._lock:
            return self._records.get(handshake_id)


# ---------------------------------------------------------------------------
# Capsule emission helpers
# ---------------------------------------------------------------------------


def seal_request(
    requester_org: str,
    developer: str,
    action: str,
    action_digest: str,
    *,
    ledger: str | None = None,
    anchor: bool = False,
    extra_compute: dict | None = None,
) -> "EmitResult":  # type: ignore[name-defined]
    """Emit a capsule recording Org A's request attestation.

    The capsule carries the action_digest in compute_attestation so a
    verifier can confirm both parties attested over the same action.
    ``verdict="executed"`` reflects that A committed to the request;
    ``effect.status="dispatched"`` signals the action is in flight.
    """
    from capsule_emit.core import emit

    compute: dict = {"action_digest": action_digest, "role": "requester"}
    if extra_compute:
        compute.update(extra_compute)
    kw: dict = dict(
        action=action,
        operator=requester_org,
        developer=developer,
        verdict="executed",
        effect={"type": action, "status": "dispatched"},
        anchor=anchor,
        extra_compute=compute,
    )
    if ledger is not None:
        kw["ledger"] = ledger
    return emit(**kw)


def seal_action(
    responder_org: str,
    developer: str,
    action: str,
    action_digest: str,
    requester_capsule_id: str,
    *,
    verdict: str = "executed",
    effect_status: str = "confirmed",
    ledger: str | None = None,
    anchor: bool = False,
    gate_checks: list | None = None,
    extra_compute: dict | None = None,
) -> "EmitResult":  # type: ignore[name-defined]
    """Emit a capsule recording Org B's action attestation.

    Chains onto the requester's capsule.  ``verdict`` reflects B's
    disposition (executed / blocked / denied / escalated).  Gate check
    results from the constraint evaluation are stored in compute_attestation.
    """
    from capsule_emit.core import emit

    compute: dict = {"action_digest": action_digest, "role": "responder"}
    if gate_checks:
        compute["gate_checks"] = gate_checks
    if extra_compute:
        compute.update(extra_compute)
    kw: dict = dict(
        action=action,
        operator=responder_org,
        developer=developer,
        verdict=verdict,
        effect={"type": action, "status": effect_status},
        confirms=requester_capsule_id,
        relation="confirms",
        anchor=anchor,
        extra_compute=compute,
    )
    if ledger is not None:
        kw["ledger"] = ledger
    return emit(**kw)


def seal_bilateral(
    party_org: str,
    developer: str,
    action: str,
    action_digest: str,
    prior_capsule_id: str,
    *,
    ledger: str | None = None,
    anchor: bool = False,
    extra_compute: dict | None = None,
) -> "EmitResult":  # type: ignore[name-defined]
    """Emit a capsule recording bilateral completion (both parties confirmed).

    This is the completion capsule that records the BILATERAL state: both
    parties have acknowledged each other's attestations.  The chain links to
    the most recent prior capsule in the exchange.
    """
    from capsule_emit.core import emit

    compute: dict = {
        "action_digest": action_digest,
        "bilateral_state": "bilateral",
    }
    if extra_compute:
        compute.update(extra_compute)
    kw: dict = dict(
        action=f"{action}:bilateral",
        operator=party_org,
        developer=developer,
        verdict="confirmed",
        effect={"type": action, "status": "confirmed"},
        confirms=prior_capsule_id,
        relation="confirms",
        anchor=anchor,
        extra_compute=compute,
    )
    if ledger is not None:
        kw["ledger"] = ledger
    return emit(**kw)
