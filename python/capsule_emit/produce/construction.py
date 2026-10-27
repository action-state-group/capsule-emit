# SPDX-License-Identifier: Apache-2.0
"""Foreign-artifact and composition bindings, mirroring Go ``construction.go``.

``carry`` and ``received`` digest the exact transmitted bytes and never parse
them. ``who``/``can``/``did``/``audit`` bind existing Capsules to the closed
slot vocabulary and ``build_composition`` emits them in canonical slot order.
"""
from __future__ import annotations

import hashlib

from .build import _digest_reference, _is_hex64, build_with_binding
from .model import BuiltPayload, Input, Result, SlotMember
from .verify import verify_capsule

__all__ = ["carry", "received", "who", "can", "did", "audit", "build_composition"]

_DIGEST_ALG = "SHA-256"
_FOREIGN_ARTIFACT_TYPE = "foreign-artifact"
_CAPSULE_REFERENCE_TYPE = "capsule"
_SLOTS = ("who", "can", "did", "audit")


def carry(input: Input, artifact: bytes) -> BuiltPayload:
    """Build a Capsule binding an opaque foreign artifact exactly as received."""
    return received(input, artifact, _FOREIGN_ARTIFACT_TYPE)


def received(input: Input, artifact: bytes, artifact_type: str) -> BuiltPayload:
    """Build a Capsule binding an opaque foreign artifact and its
    caller-declared registered CPB type. ``artifact`` must be bytes: text is
    not encoded on the caller's behalf, since the digest is over the exact
    transmitted bytes."""
    if not isinstance(artifact, (bytes, bytearray, memoryview)):
        raise TypeError("received artifact must be bytes")
    data = bytes(artifact)
    if not data:
        raise ValueError("received artifact must not be empty")
    if not isinstance(artifact_type, str) or not artifact_type.strip():
        raise ValueError("received artifact type must be a non-empty string")
    digest = hashlib.sha256(data).hexdigest()
    binding = {
        "carried_artifact": _digest_reference(artifact_type, _DIGEST_ALG, digest),
        "carried_input_digest": digest,
    }
    return build_with_binding(input, binding)


def _slot(slot: str, member: BuiltPayload | Result) -> SlotMember:
    if isinstance(member, BuiltPayload):
        return SlotMember(slot=slot, capsule_id=member.capsule_id, json=bytes(member.json))
    if isinstance(member, Result):
        return SlotMember(slot=slot, capsule_id=member.capsule_id, json=bytes(member.payload))
    raise TypeError(f"{slot}() takes a BuiltPayload or Result")


def who(member: BuiltPayload | Result) -> SlotMember:
    """Assign an existing Capsule to the identity or pedigree slot."""
    return _slot("who", member)


def can(member: BuiltPayload | Result) -> SlotMember:
    """Assign an existing Capsule to the authority or mandate slot."""
    return _slot("can", member)


def did(member: BuiltPayload | Result) -> SlotMember:
    """Assign an existing Capsule to the performed-action slot."""
    return _slot("did", member)


def audit(member: BuiltPayload | Result) -> SlotMember:
    """Assign an existing Capsule to the independent-review slot."""
    return _slot("audit", member)


def build_composition(input: Input, *members: SlotMember) -> BuiltPayload:
    """Build a Capsule binding existing signature-free Capsules into WHO, CAN,
    DID and AUDIT roles. Members are emitted in canonical slot order whatever
    the argument order; empty membership, duplicate slots, duplicate IDs and
    members that do not verify are rejected."""
    if not members:
        raise ValueError("composition requires at least one slot member")
    by_slot: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for index, member in enumerate(members):
        if not isinstance(member, SlotMember) or member.slot not in _SLOTS:
            raise ValueError(f"composition member {index} has invalid slot")
        slot = member.slot
        if slot in by_slot:
            raise ValueError(f"composition duplicates {slot} slot")
        if not _is_hex64(member.capsule_id):
            raise ValueError(f"composition {slot} member has malformed Capsule ID")
        try:
            verified = verify_capsule(member.json)
        except (TypeError, ValueError):
            verified = None
        if verified is None or verified.capsule_id != member.capsule_id:
            raise ValueError(f"composition {slot} member is not a matching verified format-4 Capsule")
        if member.capsule_id in seen_ids:
            raise ValueError(
                f"composition {slot} slot duplicates Capsule ID {member.capsule_id} from {seen_ids[member.capsule_id]} slot"
            )
        seen_ids[member.capsule_id] = slot
        by_slot[slot] = member.capsule_id
    composed = [
        _digest_reference(_CAPSULE_REFERENCE_TYPE, _DIGEST_ALG, by_slot[slot], slot) for slot in _SLOTS if slot in by_slot
    ]
    return build_with_binding(input, {"composed_members": composed})
