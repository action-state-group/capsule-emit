# SPDX-License-Identifier: Apache-2.0
"""Settlement records: two parties each record the same payment.

Reference producer for the agent settlement profile draft
(draft-mih-agent-settlement-profile-00, in progress). The payer and the payee
are two independent sealers. Each seals only what its own wallet or system
observed, in its own log, under its own key. A reader joins the two halves on
a typed payment reference afterwards. Neither side signs the other's claim.

The legs, chained by digest within each party's log::

    terms -> payer_observed -> payee_observed -> delivered

- ``terms``: the agreed terms, carried as ``terms_digest`` (and any already
  signed offer or mandate wrapped by digest, never re-signed).
- ``payer_observed``: what the payer's wallet returned for the payment.
- ``payee_observed``: what the payee's wallet or facilitator returned.
- ``delivered``: a digest of the delivered content, bound to the same
  ``terms_digest``. Either side may seal one; if both do, the digests must
  agree.

Public API
----------
Building (pure, no I/O, validates everything):
  build_observation(...)  -- one leg's observation block
  terms_digest(terms)     -- SHA-256 over the RFC 8785 JCS of a terms object
  wrap(obj, type=...)     -- a digest reference to an existing signed object
  amount(value, asset_code, asset_scale) -- the exact amount triple

Sealing:
  seal_observation(observation, ...) -- seal one block as a capsule

Joining (offline, over sealed capsule dicts from both parties):
  join(capsules, trusted_keys=None) -> (list[SettlementJoin], list[RefusedRecord])

What a join never says: one side's record missing reads ``payer_only`` or
``payee_only`` -- "only one side is held here", never "unpaid" or "not
delivered". A record that fails verification is refused and listed, never
silently dropped and never joined. Two keys are not proof of two parties: a
role is what the sealer claims. Pin keys to roles with ``join(...,
trusted_keys=...)`` when the reader knows them.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from agent_action_capsule.canonical import jcs

from .core import EmitResult, _emit_capsule

__all__ = [
    "SETTLEMENT_EXTENSION_KEY",
    "SETTLEMENT_SCHEMA",
    "ROLES",
    "LEGS",
    "STATUSES",
    "ISO20022_STATUS",
    "PAYMENT_REF_TYPES",
    "MONEY_MEMBERS",
    "SettlementError",
    "SettlementJoin",
    "RefusedRecord",
    "amount",
    "terms_digest",
    "wrap",
    "build_observation",
    "validate_observation",
    "seal_observation",
    "join",
]

#: The ``model_attestation.compute_attestation`` key the observation rides
#: under. Versioned with the draft: renamed when the profile is adopted.
SETTLEMENT_EXTENSION_KEY = "x-settlement-v0"
SETTLEMENT_SCHEMA = "settlement-profile-00"

ROLES = ("payer", "payee")
LEGS = ("terms", "payer_observed", "payee_observed", "delivered")

#: Observation status, per observed leg. Terms and delivered legs carry none.
STATUSES = {
    "payer_observed": ("pending", "settled", "rejected"),
    "payee_observed": ("pending", "received", "rejected"),
}

#: The ISO 20022 code a bank or compliance reviewer would read for each
#: status: pacs.002 TxSts for the payer side, pacs.002 ACCC / camt.054
#: BOOK for funds credited to the payee.
ISO20022_STATUS = {
    ("payer_observed", "pending"): "PDNG",
    ("payer_observed", "settled"): "ACSC",
    ("payer_observed", "rejected"): "RJCT",
    ("payee_observed", "pending"): "PDNG",
    ("payee_observed", "received"): "ACCC",
    ("payee_observed", "rejected"): "RJCT",
}

_SUCCESS = {"payer_observed": "settled", "payee_observed": "received"}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EVM_TX = re.compile(r"^0x[0-9a-f]{64}$")
_CAIP2 = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TYPE_TOKEN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\.[a-z0-9_]+$")
#: Binding names keep the upstream field's own case (``erc8004.agentId``).
_BINDING_KEY = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\.[A-Za-z0-9_]+$")
_DIGITS = re.compile(r"^(0|[1-9][0-9]*)$")


class SettlementError(ValueError):
    """An observation that the profile does not allow."""


def _check_x402(ref: dict) -> None:
    network = ref.get("network")
    if not isinstance(network, str) or not _CAIP2.match(network):
        raise SettlementError("x402.transaction needs a CAIP-2 network, e.g. eip155:84532")
    if network.startswith("eip155:") and not _EVM_TX.match(ref["value"]):
        raise SettlementError("an EVM transaction hash is 0x + 64 lowercase hex")


def _check_hex64(ref: dict) -> None:
    if not _HEX64.match(ref["value"]):
        raise SettlementError(f"{ref['type']} is 64 lowercase hex")


def _check_uetr(ref: dict) -> None:
    if not _UUID.match(ref["value"]):
        raise SettlementError("iso20022.uetr is a lowercase UUID")


def _check_e2eid(ref: dict) -> None:
    if len(ref["value"]) > 35:
        raise SettlementError("iso20022.end_to_end_id is at most 35 characters")


def _check_https(ref: dict) -> None:
    if not ref["value"].startswith("https://"):
        raise SettlementError(f"{ref['type']} is an https URL")


def _no_extra_check(ref: dict) -> None:
    return None


#: The known payment reference types and the check each one's value gets.
#: The registry is open: any other type token of the same shape is accepted
#: and joins on exact equality, with no value check.
PAYMENT_REF_TYPES = {
    "x402.transaction": _check_x402,
    "ln.payment_hash": _check_hex64,
    "bolt12.invoice_payment_hash": _check_hex64,
    "ap2.transaction_id": _no_extra_check,
    "ap2.payment_id": _no_extra_check,
    "ap2.network_confirmation_id": _no_extra_check,
    "acp.order_id": _no_extra_check,
    "ucp.order_id": _no_extra_check,
    "mpp.receipt_reference": _no_extra_check,
    "iso20022.uetr": _check_uetr,
    "iso20022.end_to_end_id": _check_e2eid,
    "open_payments.incoming_payment": _check_https,
}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def terms_digest(terms: Any) -> str:
    """SHA-256 (lowercase hex) over the RFC 8785 JCS of *terms*.

    Floats are refused by the JCS step; write amounts as digit strings.
    """
    return _sha256_hex(jcs(terms))


def wrap(obj: Any, *, type: str) -> dict:
    """A digest reference to an object someone already signed.

    Bytes are digested as given (a JWS, a COSE object, the raw EIP-712
    signature payload). Anything else is digested over its JCS. The object
    itself is not carried and not re-signed.
    """
    if not isinstance(type, str) or not _TYPE_TOKEN.match(type):
        raise SettlementError(f"wrap type must be a dotted token, got {type!r}")
    if isinstance(obj, (bytes, bytearray, memoryview)):
        digest = _sha256_hex(bytes(obj))
        form = "bytes"
    else:
        digest = _sha256_hex(jcs(obj))
        form = "jcs"
    return {"type": type, "form": form, "digest_alg": "sha-256", "digest": digest}


def amount(value: int | str, asset_code: str, asset_scale: int) -> dict:
    """The exact amount triple ``{value, assetCode, assetScale}``.

    *value* is an integer count of the smallest unit (1 USDC at scale 6 is
    ``"1000000"``). Floats are refused.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SettlementError("amount value is an integer or a digit string, never a float")
    text = str(value)
    if not _DIGITS.match(text):
        raise SettlementError(f"amount value must be a non-negative integer, got {text!r}")
    if not isinstance(asset_code, str) or not asset_code:
        raise SettlementError("assetCode is a non-empty string")
    if isinstance(asset_scale, bool) or not isinstance(asset_scale, int) or not 0 <= asset_scale <= 255:
        raise SettlementError("assetScale is an integer from 0 to 255")
    return {"value": text, "assetCode": asset_code, "assetScale": asset_scale}


def _check_payment_ref(ref: Any) -> None:
    if not isinstance(ref, dict):
        raise SettlementError("payment_ref is an object")
    allowed = {"type", "value", "network"}
    extra = set(ref) - allowed
    if extra:
        raise SettlementError(f"payment_ref has unknown fields {sorted(extra)}")
    rtype, value = ref.get("type"), ref.get("value")
    if not isinstance(rtype, str) or not _TYPE_TOKEN.match(rtype):
        raise SettlementError(f"payment_ref.type must be a dotted token, got {rtype!r}")
    if not isinstance(value, str) or not value:
        raise SettlementError("payment_ref.value is a non-empty string")
    if "network" in ref and (not isinstance(ref["network"], str) or not _CAIP2.match(ref["network"])):
        raise SettlementError("payment_ref.network is a CAIP-2 chain id")
    PAYMENT_REF_TYPES.get(rtype, _no_extra_check)(ref)


#: The money members each leg may carry. ``terms``: the price and an optional
#: declared bound on the payee's receive fee. ``payer_observed``: the amount
#: sent toward the payee and the routing fee paid on top of it.
#: ``payee_observed``: the amount that arrived and the receive fee deducted
#: before it arrived. Consistency: payer amount == received + receive_fee.
MONEY_MEMBERS = {
    "terms": ("amount", "receive_fee_max"),
    "payer_observed": ("amount", "routing_fee"),
    "payee_observed": ("received", "receive_fee"),
    "delivered": (),
}
_ALL_MONEY = ("amount", "routing_fee", "received", "receive_fee", "receive_fee_max")


def _check_amount(value: Any, name: str = "amount") -> None:
    if not isinstance(value, dict) or set(value) != {"value", "assetCode", "assetScale"}:
        raise SettlementError(f"{name} is exactly {{value, assetCode, assetScale}}")
    amount(value["value"], value["assetCode"], value["assetScale"])
    if not isinstance(value["value"], str):
        raise SettlementError("a sealed amount value is a digit string")


def _check_wrapped(items: Any) -> None:
    if not isinstance(items, list):
        raise SettlementError("wrapped is a list")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"type", "form", "digest_alg", "digest"}:
            raise SettlementError("each wrapped entry is exactly {type, form, digest_alg, digest}")
        if not isinstance(item["type"], str) or not _TYPE_TOKEN.match(item["type"]):
            raise SettlementError(f"wrapped type must be a dotted token, got {item['type']!r}")
        if item["form"] not in ("bytes", "jcs") or item["digest_alg"] != "sha-256":
            raise SettlementError("wrapped form is bytes or jcs, digest_alg is sha-256")
        if not isinstance(item["digest"], str) or not _HEX64.match(item["digest"]):
            raise SettlementError("wrapped digest is 64 lowercase hex")


def _check_bindings(bindings: Any) -> None:
    if not isinstance(bindings, dict):
        raise SettlementError("bindings is an object of string values")
    for key, value in bindings.items():
        if not isinstance(key, str) or not _BINDING_KEY.match(key):
            raise SettlementError(f"binding names are <namespace>.<field>, got {key!r}")
        if not isinstance(value, str) or not value:
            raise SettlementError(f"binding {key} is a non-empty string")


_REQUIRED = ("schema", "role", "leg", "payment_ref", "terms_digest")
_OPTIONAL = ("status", *_ALL_MONEY, "wrapped", "delivered_digest", "bindings", "counterparty_capsule_id")


def validate_observation(obs: Any) -> None:
    """Raise :class:`SettlementError` unless *obs* is a well-formed block."""
    if not isinstance(obs, dict):
        raise SettlementError("an observation is an object")
    missing = [k for k in _REQUIRED if k not in obs]
    if missing:
        raise SettlementError(f"observation is missing {missing}")
    extra = set(obs) - set(_REQUIRED) - set(_OPTIONAL)
    if extra:
        raise SettlementError(f"observation has unknown fields {sorted(extra)}")
    if obs["schema"] != SETTLEMENT_SCHEMA:
        raise SettlementError(f"schema is {SETTLEMENT_SCHEMA!r}")
    role, leg = obs["role"], obs["leg"]
    if role not in ROLES:
        raise SettlementError(f"role is one of {ROLES}")
    if leg not in LEGS:
        raise SettlementError(f"leg is one of {LEGS}")
    if leg == "payer_observed" and role != "payer":
        raise SettlementError("only the payer seals payer_observed")
    if leg == "payee_observed" and role != "payee":
        raise SettlementError("only the payee seals payee_observed")
    _check_payment_ref(obs["payment_ref"])
    if not isinstance(obs["terms_digest"], str) or not _HEX64.match(obs["terms_digest"]):
        raise SettlementError("terms_digest is 64 lowercase hex")
    if leg in STATUSES:
        if obs.get("status") not in STATUSES[leg]:
            raise SettlementError(f"{leg} status is one of {STATUSES[leg]}")
    elif "status" in obs:
        raise SettlementError(f"a {leg} leg carries no status")
    if leg == "delivered":
        digest = obs.get("delivered_digest")
        if not isinstance(digest, str) or not _HEX64.match(digest):
            raise SettlementError("a delivered leg carries delivered_digest (64 lowercase hex)")
    elif "delivered_digest" in obs:
        raise SettlementError("only a delivered leg carries delivered_digest")
    for name in _ALL_MONEY:
        if name in obs:
            if name not in MONEY_MEMBERS[leg]:
                raise SettlementError(f"a {leg} leg carries no {name}")
            _check_amount(obs[name], name)
    if "wrapped" in obs:
        _check_wrapped(obs["wrapped"])
    if "bindings" in obs:
        _check_bindings(obs["bindings"])
    if "counterparty_capsule_id" in obs:
        cid = obs["counterparty_capsule_id"]
        if not isinstance(cid, str) or not _HEX64.match(cid):
            raise SettlementError("counterparty_capsule_id is 64 lowercase hex")


def build_observation(
    *,
    role: str,
    leg: str,
    payment_ref: dict,
    terms_digest: str,
    status: str | None = None,
    amount: dict | None = None,
    routing_fee: dict | None = None,
    received: dict | None = None,
    receive_fee: dict | None = None,
    receive_fee_max: dict | None = None,
    wrapped: Iterable[dict] = (),
    delivered_digest: str | None = None,
    bindings: dict[str, str] | None = None,
    counterparty_capsule_id: str | None = None,
) -> dict:
    """Build and validate one leg's observation block.

    *payment_ref* is ``{"type", "value"}`` plus ``"network"`` (CAIP-2) where
    the type needs one. An EVM transaction hash is lowercased here; every
    other value is kept exactly as given, and the join compares bytes.

    *bindings* names what else this record is bound to, as dotted keys:
    an agent registry id, a request hash, a facilitator id.

    *counterparty_capsule_id* cites the other side's record when this side
    has already seen it. It is a citation, not a countersignature.

    Money members (each an :func:`amount` triple) by leg: ``terms`` takes
    *amount* (the price) and optionally *receive_fee_max*; ``payer_observed``
    takes *amount* (sent toward the payee) and *routing_fee* (paid on top);
    ``payee_observed`` takes *received* and *receive_fee* (deducted before
    arrival). Record a zero fee as a zero amount: a missing fee is unknown,
    not zero.
    """
    ref = dict(payment_ref)
    if ref.get("type") == "x402.transaction" and isinstance(ref.get("value"), str):
        if str(ref.get("network", "")).startswith("eip155:"):
            ref["value"] = ref["value"].lower()
    obs: dict[str, Any] = {
        "schema": SETTLEMENT_SCHEMA,
        "role": role,
        "leg": leg,
        "payment_ref": ref,
        "terms_digest": terms_digest,
    }
    if status is not None:
        obs["status"] = status
    for name, value in (("amount", amount), ("routing_fee", routing_fee), ("received", received),
                        ("receive_fee", receive_fee), ("receive_fee_max", receive_fee_max)):
        if value is not None:
            obs[name] = value
    wrapped = list(wrapped)
    if wrapped:
        obs["wrapped"] = wrapped
    if delivered_digest is not None:
        obs["delivered_digest"] = delivered_digest
    if bindings:
        obs["bindings"] = dict(bindings)
    if counterparty_capsule_id is not None:
        obs["counterparty_capsule_id"] = counterparty_capsule_id
    validate_observation(obs)
    return obs


def seal_observation(
    observation: dict,
    *,
    operator: str = "",
    developer: str = "",
    prior: str | None = None,
    **kwargs: Any,
) -> EmitResult:
    """Seal one observation as a capsule in the caller's own log.

    *prior* is the capsule id of this party's previous leg for the same
    payment; the record chains onto it with relation ``follows``. Other
    keywords (``ledger``, ``witness``, ``signing_key_path``, ...) pass
    through to the capsule producer.
    """
    validate_observation(observation)
    return _emit_capsule(
        f"settlement.{observation['leg']}",
        operator=operator,
        developer=developer,
        action_type="fyi",
        confirms=prior,
        relation="follows" if prior is not None else None,
        extra_compute={SETTLEMENT_EXTENSION_KEY: observation},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefusedRecord:
    """A capsule the join would not use, and why."""

    capsule_id: str | None
    reason: str


@dataclass
class SettlementJoin:
    """Both parties' records for one payment reference.

    ``state``:

    - ``agreed``: a payer-role and a payee-role observed leg are both held,
      the payer and payee keys disjoint and all pinned to their role by
      ``trusted_keys`` (a role may have several trusted keys, e.g. after
      rotation); each side's final status is success, the money reconciles
      (see :func:`join`) with every fee member present, and every compared
      field matches.
    - ``agreed_untrusted``: the same, but ``join`` was given no
      ``trusted_keys``, so nothing ties either key to a real party.
    - ``payer_only`` / ``payee_only``: only one side's observed leg is held
      here. Says nothing about the other side's books.
    - ``differs``: both sides are held and at least one compared field
      disagrees or is missing; ``differences`` names each one.
    - ``not_independent``: one key signed records under both roles, so this
      is one key's account of both sides.
    - ``no_observation``: only terms or delivered legs are held.

    Distinct keys are not proof of distinct parties: a role is what the
    sealer claims, and anyone can hold two keys. Pass ``trusted_keys`` to
    :func:`join` to pin which keys may speak for each role; several trusted
    keys under one role are fine (rotation). Without ``trusted_keys``, more
    than one key under a role is a difference (``payer.keys`` /
    ``payee.keys``).

    ``differences`` may be non-empty on a one-sided state too: one party's
    own legs that disagree with each other.

    ``delivery``: ``matched`` (both roles sealed a delivered leg with the
    same digest, under disjoint keys), ``not_independent`` (one key sealed
    both), ``payee_only``, ``payer_only``, ``differs``, or ``none``.
    """

    payment_ref: dict
    state: str
    delivery: str
    differences: list[str] = field(default_factory=list)
    payer_capsule_ids: list[str] = field(default_factory=list)
    payee_capsule_ids: list[str] = field(default_factory=list)
    iso20022: dict[str, str] = field(default_factory=dict)


def _ref_key(ref: dict) -> bytes:
    return jcs(ref)


def _block(capsule: dict) -> Any:
    model = capsule.get("model_attestation")
    compute = model.get("compute_attestation") if isinstance(model, dict) else None
    return compute.get(SETTLEMENT_EXTENSION_KEY) if isinstance(compute, dict) else None


_KEY_ID = re.compile(r"^[0-9a-f]{64}$")


def _verify(capsule: dict) -> str | None:
    """None if *capsule*'s content recomputes and its producer signature verifies.

    Checked one record at a time: a leg's chain parent may be in another
    file, or deliberately left out, and that is not a reason to refuse it.
    ``key_id`` must be lowercase hex, so that comparing keys as strings is
    comparing keys.
    """
    from .signing import AuthorshipVerdict, verify_capsule_signature_tristate
    from .verification import verify_capsule

    if not isinstance(capsule.get("key_id"), str) or not _KEY_ID.match(capsule["key_id"]):
        return "key_id is not 64 lowercase hex"
    try:
        content_ok = verify_capsule(capsule).ok
        authorship, _ = verify_capsule_signature_tristate(capsule)
    except Exception as exc:  # a malformed record must be refused, not crash the join
        return f"verification raised {type(exc).__name__}"
    if not content_ok:
        return "capsule content does not verify"
    if authorship is not AuthorshipVerdict.AUTHORED:
        return f"producer signature {authorship.name.lower()}"
    return None


def _compare(field_name: str, values: list[Any], out: list[str]) -> None:
    distinct = {jcs(v) for v in values if v is not None}
    if len(distinct) > 1:
        out.append(field_name)


def join(
    capsules: Iterable[dict | EmitResult],
    *,
    trusted_keys: dict[str, Iterable[str]] | None = None,
) -> tuple[list[SettlementJoin], list[RefusedRecord]]:
    """Join settlement records from both parties, offline.

    Every capsule is verified first, on its own (``capsule_id`` recomputes
    and the producer signature verifies; a chain parent need not be
    present). A capsule that fails, or carries no valid settlement block, is
    returned in the refused list and takes no part in any join.

    Returns ``(joins, refused)``: one :class:`SettlementJoin` per payment
    reference, and every capsule that took no part, with the reason.

    *trusted_keys* maps a role to the ``key_id`` values allowed to speak for
    it. When given, a record whose role has no entry, or whose key is not
    listed for its role, is refused and listed, and takes no part in the
    join: the trusted records alone decide it, and a full match reads
    ``agreed``.
    Without it, the same match reads ``agreed_untrusted``.

    The same capsule passed more than once (same ``capsule_id`` and
    ``key_id``) counts once.

    Records are grouped on the exact JCS bytes of ``payment_ref``: values
    must match byte for byte (only an EVM transaction hash is lowercased, by
    :func:`build_observation`). Within a group the compared fields are
    ``terms_digest``, the delivered digests, each side's final observed
    status, and the money. Money is compared as exact rationals
    (``value / 10**assetScale``) within one ``assetCode``, never as raw
    equality across sides: the payer's ``amount`` must equal the payee's
    ``received + receive_fee``, or, when the payee recorded no
    ``receive_fee``, the gap must sit within a ``receive_fee_max`` declared
    on the terms. A missing fee with no declared bound is a difference.
    """
    trusted = {role: set(keys) for role, keys in trusted_keys.items()} if trusted_keys is not None else None
    refused: list[RefusedRecord] = []
    groups: dict[bytes, list[dict]] = {}
    order: list[bytes] = []
    seen: set[tuple[str, str]] = set()
    for item in capsules:
        capsule = item.capsule if isinstance(item, EmitResult) else item
        if not isinstance(capsule, dict):
            refused.append(RefusedRecord(None, "not a capsule object"))
            continue
        cid = capsule.get("capsule_id")
        reason = _verify(capsule)
        if reason is not None:
            refused.append(RefusedRecord(cid, reason))
            continue
        block = _block(capsule)
        if block is None:
            refused.append(RefusedRecord(cid, "no settlement block"))
            continue
        try:
            validate_observation(block)
        except SettlementError as exc:
            refused.append(RefusedRecord(cid, f"settlement block refused: {exc}"))
            continue
        if trusted is not None and capsule["key_id"] not in trusted.get(block["role"], set()):
            refused.append(RefusedRecord(cid, f"key is not trusted for role {block['role']}"))
            continue
        identity = (capsule["capsule_id"], capsule["key_id"])
        if identity in seen:
            continue
        seen.add(identity)
        key = _ref_key(block["payment_ref"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(capsule)
    return [_join_group(groups[k], trusted=trusted is not None) for k in order], refused


def _final_statuses(statuses: list[str]) -> set[str]:
    """A side's final status: ``pending`` is superseded by any outcome."""
    outcomes = set(statuses) - {"pending"}
    return outcomes or set(statuses)


def _qty(triple: dict) -> Fraction:
    return Fraction(int(triple["value"]), 10 ** triple["assetScale"])


def _one(values: list[Fraction], name: str, out: list[str]) -> Fraction | None:
    """The single value *values* agree on, else None (and a difference if they disagree)."""
    distinct = set(values)
    if len(distinct) > 1:
        out.append(name)
    return next(iter(distinct)) if len(distinct) == 1 else None


def _money(blocks: list[dict], observed: dict[str, list[dict]], out: list[str]) -> None:
    triples = [b[n] for b in blocks for n in _ALL_MONEY if n in b]
    if len({t["assetCode"] for t in triples}) > 1:
        out.append("asset")
        return
    terms = [b for b in blocks if b["leg"] == "terms"]
    price = _one([_qty(b["amount"]) for b in terms if "amount" in b], "terms.amount", out)
    bound = _one([_qty(b["receive_fee_max"]) for b in terms if "receive_fee_max" in b],
                 "terms.receive_fee_max", out)

    payer, payee = observed["payer"], observed["payee"]
    for name, side, legs in (("amount", "payer", payer), ("routing_fee", "payer", payer),
                             ("received", "payee", payee)):
        if any(name not in b for b in legs):
            out.append(f"{side}_observed.{name} missing")
    sent = _one([_qty(b["amount"]) for b in payer if "amount" in b], "payer_observed.amount", out)
    got = _one([_qty(b["received"]) for b in payee if "received" in b], "payee_observed.received", out)
    fees = [_qty(b["receive_fee"]) for b in payee if "receive_fee" in b]
    fee = _one(fees, "payee_observed.receive_fee", out)
    if fees and len(fees) < len(payee):
        out.append("payee_observed.receive_fee missing")
    if price is not None and sent is not None and sent != price:
        out.append("payer_observed.amount != terms.amount")
    if fee is not None and bound is not None and fee > bound:
        out.append("payee_observed.receive_fee > terms.receive_fee_max")
    if sent is None or got is None:
        return
    if fee is not None:
        if got + fee != sent:
            out.append("amount")
    elif not fees:
        if bound is None:
            out.append("payee_observed.receive_fee missing")
        elif not 0 <= sent - got <= bound:
            out.append("amount")


def _join_group(capsules: list[dict], *, trusted: bool) -> SettlementJoin:
    blocks = [(c, _block(c)) for c in capsules]
    ref = blocks[0][1]["payment_ref"]
    differences: list[str] = []

    def keys_for(role: str, leg: str | None = None) -> set[str]:
        return {c["key_id"] for c, b in blocks if b["role"] == role and (leg is None or b["leg"] == leg)}

    _compare("terms_digest", [b["terms_digest"] for _, b in blocks], differences)

    for side in ROLES:
        if not trusted and len(keys_for(side)) > 1:
            differences.append(f"{side}.keys")

    observed = {side: [b for _, b in blocks if b["leg"] == f"{side}_observed"] for side in ROLES}
    final = {side: _final_statuses([b["status"] for b in observed[side]]) for side in ROLES}
    _money(
        [b for _, b in blocks],
        {side: [b for b in observed[side] if b["status"] in final[side]] for side in ROLES},
        differences,
    )
    for side in ROLES:
        if len(final[side]) > 1:
            differences.append(f"{side}_observed.status")

    delivered = {
        side: [b["delivered_digest"] for _, b in blocks if b["leg"] == "delivered" and b["role"] == side]
        for side in ROLES
    }
    if len({*delivered["payer"], *delivered["payee"]}) > 1:
        differences.append("delivered_digest")
        delivery = "differs"
    elif delivered["payer"] and delivered["payee"]:
        shared = keys_for("payer", "delivered") & keys_for("payee", "delivered")
        delivery = "not_independent" if shared else "matched"
    elif delivered["payee"]:
        delivery = "payee_only"
    elif delivered["payer"]:
        delivery = "payer_only"
    else:
        delivery = "none"

    iso = {
        f"{side}_observed": ISO20022_STATUS[(f"{side}_observed", next(iter(final[side])))]
        for side in ROLES
        if len(final[side]) == 1
    }

    have_payer, have_payee = bool(observed["payer"]), bool(observed["payee"])
    if have_payer and have_payee:
        if keys_for("payer") & keys_for("payee"):
            state = "not_independent"
        else:
            for side in ROLES:
                success = _SUCCESS[f"{side}_observed"]
                if final[side] != {success}:
                    differences.append(f"{side}_observed.status=" + ",".join(sorted(final[side])))
            state = "differs" if differences else ("agreed" if trusted else "agreed_untrusted")
    elif have_payer:
        state = "payer_only"
    elif have_payee:
        state = "payee_only"
    else:
        state = "no_observation"

    return SettlementJoin(
        payment_ref=ref,
        state=state,
        delivery=delivery,
        differences=sorted(set(differences)),
        payer_capsule_ids=[c["capsule_id"] for c, b in blocks if b["role"] == "payer"],
        payee_capsule_ids=[c["capsule_id"] for c, b in blocks if b["role"] == "payee"],
        iso20022=iso,
    )
