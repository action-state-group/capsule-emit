# SPDX-License-Identifier: Apache-2.0
"""The A2A carrier of an ``evidence-commitment/1``: write the Agent Card entry and read it back.

Implements section 5.2 and steps V0 to V3 of ``spec/bindings/evidence-commitment-carriers-v0.md``
(agent-action-capsule). The entry is only a pointer: ``commitment_uri`` and a ``commitment_digest``.
The reader trusts nothing in the card. It takes the commitment's octets from ``commitment_uri`` and
verifies them on their own; the card's name, description and other claims never decide a result.

The entry is a separate ``AgentExtension``. It is never placed in the ``a2a-task-evidence/v1``
entry's ``params`` (spec V0, ``discovery_object``), and that extension is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_action_capsule.canonical import jcs
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

COMMITMENT_EXTENSION_URI = (
    "https://github.com/action-state-group/agent-action-capsule/blob/main/"
    "spec/bindings/evidence-commitment-carriers-v0.md"
)
ENTRY_DESCRIPTION = (
    "Points to this agent's signed evidence commitment; verify the commitment, not this entry"
)
PROFILES = ("evidence-commitment/1", "evidence-commitment/0")
ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: The pointer members; neither may sit in another extension's ``params`` (V0, ``discovery_object``).
_COMMITMENT_MEMBERS = {"commitment_uri", "commitment_digest"}


def commitment_digest(octets: bytes) -> str:
    """The digest a carrier writes: SHA-256 over the octets exactly as served (spec section 4)."""
    return "sha256:" + hashlib.sha256(octets).hexdigest()


def commitment_entry(commitment_uri: str, digest_or_octets: str | bytes) -> dict[str, Any]:
    """The one ``capabilities.extensions[]`` entry that advertises a commitment.

    Pass the commitment octets, or an already computed ``sha256:`` digest. Add the result to the
    card's extensions only when the operator has turned publishing on: the default is off.
    """
    digest = (
        digest_or_octets
        if isinstance(digest_or_octets, str)
        else commitment_digest(digest_or_octets)
    )
    if not commitment_uri.startswith("https://"):
        raise ValueError("commitment_uri must be an https: URI")
    if not _DIGEST.match(digest):
        raise ValueError("commitment_digest must be sha256: followed by 64 lowercase hex")
    return {
        "uri": COMMITMENT_EXTENSION_URI,
        "description": ENTRY_DESCRIPTION,
        "required": False,
        "params": {"commitment_uri": commitment_uri, "commitment_digest": digest},
    }


@dataclass
class ReadResult:
    """What a read found. ``failure`` names the first step that failed (``None`` with no commitment and no digest result means the card publishes none); ``commitment`` holds the
    parsed commitment when its own signature and staples verified, whatever the digest said."""

    failure: str | None = None
    detail: str = ""
    digest_matches: bool | None = None
    commitment: dict[str, Any] | None = None
    octets: bytes | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failure is None and self.digest_matches is True


def check_entry(card: dict[str, Any]) -> tuple[dict[str, Any] | None, ReadResult | None]:
    """V0: find the commitment entry in a card dict and check its form.

    Returns ``(entry, None)``; ``(None, None)`` when the card has no such entry; or
    ``(None, failure)`` for a malformed one (``a2a_entry``, or ``discovery_object``).
    """
    caps = card.get("capabilities") if isinstance(card, dict) else None
    exts = caps.get("extensions") if isinstance(caps, dict) else None
    if exts is None:
        return None, None
    if not isinstance(exts, list) or not all(isinstance(e, dict) for e in exts):
        return None, ReadResult("a2a_entry", "capabilities.extensions is not a list of objects")
    for ext in exts:
        if ext.get("uri") == COMMITMENT_EXTENSION_URI:
            continue
        params = ext.get("params")
        if isinstance(params, dict) and _COMMITMENT_MEMBERS & params.keys():
            return None, ReadResult("discovery_object", f"{ext.get('uri')}: commitment member in params")
    entries = [e for e in exts if e.get("uri") == COMMITMENT_EXTENSION_URI]
    if not entries:
        return None, None
    if len(entries) > 1:
        return None, ReadResult("a2a_entry", "more than one commitment entry")
    e = entries[0]
    p = e.get("params")
    if (
        e.get("required", False) is not False  # proto3 JSON omits a false bool
        or not isinstance(p, dict)
        or set(p) != {"commitment_uri", "commitment_digest"}
        or not isinstance(p["commitment_uri"], str)
        or not p["commitment_uri"].startswith("https://")
        or not isinstance(p["commitment_digest"], str)
        or not _DIGEST.match(p["commitment_digest"])
    ):
        return None, ReadResult("a2a_entry", "entry is not of the form of spec section 5.2")
    return e, None


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_commitment_octets(octets: bytes) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """V2 and V3: the commitment's own form, signature and staples. No carrier fact is read.

    This is a small shared check of the signature and staple digests; the loop's
    ``scripts/check_commitment.py`` is the full schema checker and can replace it through the
    ``verify`` argument of :func:`read_commitment`.
    """
    try:
        doc = json.loads(octets)
        if not isinstance(doc, dict) or doc.get("profile") not in PROFILES:
            return None, ("commitment_form", "not a JSON object of an evidence-commitment profile")
    except ValueError:
        return None, ("commitment_form", "not JSON")
    try:
        spki = base64.b64decode(doc["spki"], validate=True)
        if not re.fullmatch(r"[0-9a-f]{64}", doc["key_id"]):
            raise ValueError("key_id")
        key = bytes.fromhex(doc["key_id"])
        sig = doc["sig"]
        if not isinstance(sig["value"], str):
            raise TypeError("sig.value")
        if spki != ED25519_SPKI + key or sig["alg"] != "Ed25519" or sig["canon"] != "jcs":
            return None, ("key_id_spki", "key_id is not the key of spki")
    except (KeyError, TypeError, ValueError):
        return None, ("key_id_spki", "key_id, spki or sig missing or malformed")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            _b64url(sig["value"]), jcs({k: v for k, v in doc.items() if k != "sig"})
        )
    except (InvalidSignature, ValueError, TypeError):
        return None, ("commitment_signature", "sig does not verify over the JCS of the rest")
    try:
        cards = doc["history_cards"] if doc["profile"].endswith("/1") else [doc["history_card"]]
        staples = [doc["checkpoint"], *cards]
        for s in staples:
            if hashlib.sha256(base64.b64decode(s["bytes"], validate=True)).hexdigest() != s["sha256"]:
                return None, ("staple_digest", "a staple's sha256 does not match its bytes")
    except (KeyError, TypeError, ValueError):
        return None, ("staple_digest", "a staple is missing or malformed")
    return doc, None


Fetch = Callable[[str], bytes]
Verify = Callable[[bytes], tuple[dict[str, Any] | None, tuple[str, str] | None]]


def read_commitment(
    card: dict[str, Any], fetch: Fetch, verify: Verify = verify_commitment_octets
) -> ReadResult:
    """Read the commitment a card advertises. Nothing else in the card is read.

    ``fetch(uri)`` returns the octets or raises ``OSError``. A digest that does not match is
    reported as ``digest_matches=False`` and ``failure="digest_mismatch"``; the octets are still
    verified on their own (V1), so a stale pointer never rejects a commitment that verifies.
    """
    entry, bad = check_entry(card)
    if bad:
        return bad
    if entry is None:
        return ReadResult(detail="the card carries no commitment entry")  # not a V0 failure: nothing published
    params = entry["params"]
    try:
        octets = fetch(params["commitment_uri"])
    except OSError as exc:
        return ReadResult("commitment_unreachable", str(exc))
    result = ReadResult(octets=octets, digest_matches=commitment_digest(octets) == params["commitment_digest"])
    doc, err = verify(octets)
    if err:
        result.failure, result.detail = err
        return result
    result.commitment = doc
    if not result.digest_matches:
        result.failure = "digest_mismatch"
        result.detail = "the pointer is stale: the octets verify on their own"
    return result
