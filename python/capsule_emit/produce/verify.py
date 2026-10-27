# SPDX-License-Identifier: Apache-2.0
"""Capsule and Producer Envelope verification, mirroring Go ``verify.go``.

Both checks are agent-action-capsule's: Class 1 is
``agent_action_capsule.verify.verify`` and the envelope check is
``agent_action_capsule.producer_envelope.verify_producer_envelope``.
"""
from __future__ import annotations

from typing import Any

from agent_action_capsule.producer_envelope import VerificationResult as EnvelopeVerificationResult
from agent_action_capsule.producer_envelope import verify_producer_envelope
from agent_action_capsule.verify import VerificationResult
from agent_action_capsule.verify import verify as _verify_class1

from .constants import CANONICALIZATION_ID, FORMAT_VERSION
from .digest import decode_strict_json

__all__ = [
    "Class1Error",
    "EnvelopeVerificationError",
    "decode_payload",
    "verify_capsule",
    "verify_envelope",
]


class Class1Error(ValueError):
    """AAC Class 1 verification failed; ``result`` holds the findings."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        parts = [
            f"check={'none' if f.check is None else f.check} severity={f.severity} code={f.code} detail={f.detail}"
            for f in result.findings
        ]
        super().__init__("AAC Class 1 verification failed: " + "; ".join(parts))


class EnvelopeVerificationError(ValueError):
    """Producer Envelope verification failed; ``result`` holds the findings."""

    def __init__(self, result: EnvelopeVerificationResult) -> None:
        self.result = result
        parts = [f"{f.code}: {f.detail}" for f in result.findings]
        super().__init__("Producer Envelope verification failed: " + "; ".join(parts))


def decode_payload(data: Any) -> dict[str, Any]:
    """Decode Capsule JSON strictly (UTF-8, no duplicate names, no trailing
    data, bounded depth) and require a JSON object."""
    try:
        decoded = decode_strict_json(data)
    except ValueError as exc:
        raise ValueError(f"decode capsule payload: {exc}") from None
    if not isinstance(decoded, dict):
        raise ValueError("capsule payload is not an object")
    return decoded


def class1(payload: dict[str, Any]) -> VerificationResult:
    """Run AAC Class 1 over a decoded payload with the seeded registries."""
    return _verify_class1(payload)


def verify_capsule(data: Any) -> VerificationResult:
    """Decode a format-4 Capsule and run AAC Class 1 verification, raising
    :class:`Class1Error` when it fails. ``spec_version`` is deliberately not
    compared. The local-only ``signature`` and ``key_id`` fields are neither
    committed by the Capsule ID nor authenticated here: verify the signature
    with :func:`verify_envelope` and bind any ``key_id`` to its public key."""
    payload = decode_payload(data)
    if payload.get("format_version") != FORMAT_VERSION or payload.get("canonicalization_id") != CANONICALIZATION_ID:
        raise ValueError(
            "unsupported Capsule profile: only AAC format 4 with canonicalization_id "
            f'"{CANONICALIZATION_ID}" is supported'
        )
    result = class1(payload)
    if not result.ok:
        raise Class1Error(result)
    return result


def verify_envelope(capsule_id: str, data: bytes) -> EnvelopeVerificationResult:
    """Authenticate one Producer Envelope against a Capsule ID, raising
    :class:`EnvelopeVerificationError` when it fails. The result's public key
    is evidence, not an authorization decision."""
    result = verify_producer_envelope(capsule_id, data)
    if not result.ok:
        raise EnvelopeVerificationError(result)
    return result
