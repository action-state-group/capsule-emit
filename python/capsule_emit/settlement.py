# SPDX-License-Identifier: Apache-2.0
"""Settlement records: two parties each record the same payment.

Reference producer and verifier for "Two-Party Settlement Records for Agent
Payments", draft-mih-agent-settlement-records-00
(https://datatracker.ietf.org/doc/draft-mih-agent-settlement-records/).

The payer and the payee are two independent sealers. Each seals only what its
own wallet or system observed, in its own log, under its own key. A leg record
is an ordinary Capsule with a top-level ``settlement`` member::

    terms  <-terms_ref-  payer_observed, payee_observed, delivered

- ``terms``: what the payment is for and how much (sealed by the party that
  set the terms; any signed offer or mandate is wrapped by digest).
- ``payer_observed``: what the payer's system reported: ``amount`` sent toward
  the payee, ``routing_fee`` paid on top, ``status``.
- ``payee_observed``: what the payee's system reported: ``received``,
  ``receive_fee`` deducted on the receiving side, ``status``.
- ``delivered``: a ``delivery`` object, ``direction`` ``sent`` (payee) or
  ``received`` (payer), with the ``content_digest`` of the delivered octets.

Public API
----------
Building and sealing:
  amount(value, asset_code, asset_scale) -- the exact amount triple
  wrap(obj, type=...)                    -- a digest reference to an existing object
  counterparty_reference(capsule_id)     -- a ``counterparty_half`` citation
  build_leg(leg, sealer_role, ...)       -- one leg's ``settlement`` member, validated
  seal_leg(member, ...)                  -- seal it as a Capsule in the caller's log

Verifying (offline, over both parties' capsules):
  verify_settlements(capsules, key_policy=..., wrapped_objects=...) -> SettlementReport
  (agent-action-capsule's verifier, ``agent_action_capsule.settlement``,
  which also owns the draft's tables and ``structure_failures``; re-exported
  here)

The derived states are the draft's: payment ``terms_only`` / ``payer_stated`` /
``payee_stated`` / ``agreed`` / ``mismatch`` / ``unjoined``, and delivery
``none`` / ``stated`` / ``matched`` / ``mismatch``. A pair whose two observed
legs share a key is never ``agreed``: its state is ``None`` (no state of the
draft applies) and ``sealer_conflation`` is listed as a failure. Whether each
key belongs to its party is a separate result (``key_policy_applied``): without
a ``key_policy``, ``agreed`` means two distinct keys agree, nothing more. A
one-sided state says what one sealer reported, never that the other side
disagrees.
"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from typing import Any

from agent_action_capsule.canonical import jcs
from agent_action_capsule.contracts import ReferenceEntry
from agent_action_capsule.settlement import (
    DELIVERY_DIRECTIONS,
    ISO20022_STATUS,
    LEGS,
    MEMBERS,
    PAYMENT_REF_TYPES,
    ROLES,
    SETTLEMENT_MEMBER,
    SETTLEMENT_VERSION,
    STATUSES,
    WRAPPED_TYPES,
    SettlementReport,
    amount_ok,
    normal_payment_ref,
    structure_failures,
)
from agent_action_capsule.settlement import verify_settlements as _verify_settlements

from .core import EmitResult, _emit_capsule

__all__ = [
    "SETTLEMENT_MEMBER",
    "SETTLEMENT_VERSION",
    "LEGS",
    "ROLES",
    "STATUSES",
    "DELIVERY_DIRECTIONS",
    "PAYMENT_REF_TYPES",
    "WRAPPED_TYPES",
    "MEMBERS",
    "ISO20022_STATUS",
    "SettlementError",
    "SettlementReport",
    "amount",
    "wrap",
    "counterparty_reference",
    "build_leg",
    "structure_failures",
    "seal_leg",
    "verify_settlements",
]

# The draft's tables, the structural checks and the verifier are
# agent-action-capsule's (``agent_action_capsule.settlement``); this module
# builds and seals legs.
_AMOUNT_VALUE = re.compile(r"^(0|[1-9][0-9]*)$")


class SettlementError(ValueError):
    """A leg the draft does not allow. ``codes`` lists the draft's failure codes."""

    def __init__(self, codes: list[str], detail: str = "") -> None:
        self.codes = codes
        super().__init__(f"{', '.join(codes)}{': ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def amount(value: int | str, asset_code: str, asset_scale: int) -> dict:
    """The exact amount triple ``{value, assetCode, assetScale}``.

    *value* is an integer count of the smallest unit (1 USDC at scale 6 is
    ``"1000000"``; 1000 msat is ``"1000"`` at scale 11). Lightning bitcoin is
    ``assetCode`` ``BTC``; on-chain bitcoin is its CAIP-19 asset type, and the
    two are different assets. Floats are refused.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not _AMOUNT_VALUE.match(str(value)):
        raise SettlementError(["amount_not_exact"], f"value {value!r} is not a non-negative integer")
    out = {"value": str(value), "assetCode": asset_code, "assetScale": asset_scale}
    if not amount_ok(out):
        raise SettlementError(["amount_not_exact"], "assetCode is a non-empty string, assetScale 0..255")
    return out


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def wrap(obj: Any, *, type: str, include_content: bool = False) -> dict:
    """A digest reference to an object someone already produced or signed.

    *obj* as ``bytes`` is digested exactly as given: the octets the protocol
    delivered (a JWS compact string, the decoded ``PAYMENT-SIGNATURE`` header).
    Any other value is digested over its RFC 8785 form, which is the draft's
    octet rule for x402 EIP-712 envelopes ``{format, payload, signature}``.
    *include_content* carries the octets too (base64url, no padding). The
    object is never re-signed.
    """
    if type not in WRAPPED_TYPES:
        raise SettlementError(["settlement_malformed"], f"unknown wrapped type {type!r}")
    octets = bytes(obj) if isinstance(obj, (bytes, bytearray, memoryview)) else jcs(obj)
    out = {"type": type, "digest_alg": "SHA-256", "digest": hashlib.sha256(octets).hexdigest()}
    if include_content:
        out["content"] = _b64u(octets)
    return out


def counterparty_reference(capsule_id: str) -> ReferenceEntry:
    """Cite the other party's leg: custody of it, not an observation of it."""
    return ReferenceEntry(type="agent-action-capsule", digest_alg="SHA-256", digest=capsule_id,
                          citation_purpose="counterparty_half")


def build_leg(leg: str, sealer_role: str, **members: Any) -> dict:
    """Build and validate one leg's ``settlement`` member.

    Pass the draft's members by name (``terms_ref``, ``amount``,
    ``routing_fee``, ``received``, ``receive_fee``, ``payment_ref``,
    ``status``, ``observed_at``, ``deliverable``, ``valid_until``,
    ``delivery``, ``wrapped``); ``None`` values are left out. An EVM x402
    transaction hash is lowercased (its normal form). Record a fee that the
    system reported as zero as a zero amount: an absent fee states nothing.
    Raises :class:`SettlementError` with the draft's failure codes.
    """
    s: dict[str, Any] = {"version": SETTLEMENT_VERSION, "leg": leg, "sealer_role": sealer_role}
    for name, value in members.items():
        if value is None:
            continue
        if name == "payment_ref" and isinstance(value, dict):
            value = normal_payment_ref(value)
        if name == "wrapped":
            value = list(value)
            if not value:
                continue
        s[name] = value
    codes = structure_failures(s)
    if codes:
        raise SettlementError(codes)
    return s


def seal_leg(
    member: dict,
    *,
    operator: str = "",
    developer: str = "",
    prior: str | None = None,
    relation: str = "follows",
    references: Iterable[ReferenceEntry] | None = None,
    **kwargs: Any,
) -> EmitResult:
    """Seal one leg as a Capsule in the caller's own log.

    *prior* chains this leg onto the sealer's previous leg (relation
    ``follows``, or ``supersedes`` for a later observation of the same
    payment, e.g. ``pending`` then ``settled``). *references* carries
    :func:`counterparty_reference` citations. Other keywords (``ledger``,
    ``witness``, ``signing_key_path``, ...) pass through.
    """
    codes = structure_failures(member)
    if codes:
        raise SettlementError(codes)
    return _emit_capsule(
        f"settlement.{member['leg']}",
        operator=operator,
        developer=developer,
        action_type="fyi",
        confirms=prior,
        relation=relation if prior is not None else None,
        references=tuple(references) if references is not None else None,
        payload_members={SETTLEMENT_MEMBER: member},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------


def verify_settlements(
    capsules: Iterable[dict | EmitResult],
    *,
    key_policy: dict[str, str | Iterable[str]] | None = None,
    wrapped_objects: dict[str, bytes] | Iterable[bytes] | None = None,
) -> SettlementReport:
    """Derive settlement states from both parties' leg records, offline:
    ``agent_action_capsule.settlement.verify_settlements``, which also takes
    this package's :class:`~capsule_emit.core.EmitResult`."""
    return _verify_settlements(
        [item.capsule if isinstance(item, EmitResult) else item for item in capsules],
        key_policy=key_policy,
        wrapped_objects=wrapped_objects,
    )
