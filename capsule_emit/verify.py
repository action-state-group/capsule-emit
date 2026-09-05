# SPDX-License-Identifier: Apache-2.0
"""HYBRID verifier for Agent Action Capsule digest-bearing fields.

**Not an anti-equivocation check.** :func:`verify_input_digest` only confirms
a plaintext candidate matches a digest a capsule already claims to have
sealed — it says nothing about the log, checkpoint, or witness the capsule
may or may not actually be entered into. For anti-equivocation (was this
capsule honestly logged, never rewritten, never shown differently to two
parties), verify with ``capsule_emit.bundle`` (``bundle()`` /
``verify_bundle()``) instead — see ``docs/checkpoint.md``'s "Anti-
equivocation" section ([capsule-emit-witness-required-profile], per
JamesCarnley's projnanda/nandatown#217 review).

**HYBRID verifier design (CPB §5)**

The verifier operates in two tiers:

Lenient (default)
    Value-rule normative: the verifier enforces value semantics — a candidate
    carrying a raw float raises FloatInDigestError internally, which is caught
    and returned as ``NON_CONFORMING`` (not ``DIGEST_MISMATCH``). This is the
    normative tier; all conforming verifiers MUST implement it.

Strict (opt-in, ``strict=True + raw_json_bytes``)
    Token-form check: in addition to the value-rule check, the verifier
    inspects the raw JSON bytes for float tokens (e.g. ``42.0``, ``1.5``).
    A producer MUST use integer token form (``0|-?[1-9][0-9]*``); a lenient
    verifier that receives only a parsed Python dict cannot enforce this
    (declared gap — JSON parsing collapses ``42.0`` and ``42`` to the same
    value). The strict tier closes that gap by requiring the caller to supply
    the original JSON bytes alongside the parsed value.

**Declared gap**
    A lenient verifier operating on a parsed Python dict cannot distinguish
    the JSON token ``42`` from ``42.0`` — both become ``int(42)`` after
    parsing.  The strict tier is the only path that catches a producer who
    used float tokens while still producing a digest-correct capsule.

**Behavioral deltas from the as-built (pre-HYBRID) implementation**

+----------------------------------+------------------+------------------------+
| Scenario                         | As-built         | HYBRID                 |
+----------------------------------+------------------+------------------------+
| Return type                      | ``bool``         | ``InputDigestResult``  |
| Float in candidate (lenient)     | ``False``        | NON_CONFORMING         |
| Wrong value (conforming input)   | ``False``        | DIGEST_MISMATCH        |
| Absent stored digest             | ``False``        | DIGEST_MISMATCH        |
| Correct value                    | ``True``         | VERIFIED               |
| Float token in raw bytes (strict)| (no strict tier) | NON_CONFORMING         |
+----------------------------------+------------------+------------------------+

``is True`` / ``is False`` identity checks on the old ``bool`` return break;
update callers to plain truthiness (``if result:`` / ``if not result:``) or
use ``result.ok`` directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VerifyReason(str, Enum):
    """Verdict reason returned by :func:`verify_input_digest`."""

    VERIFIED = "verified"
    """Digest matches and the candidate is rule-conforming."""

    DIGEST_MISMATCH = "digest_mismatch"
    """Candidate is rule-conforming but does not match the sealed digest."""

    NON_CONFORMING = "non_conforming"
    """Candidate violates the CPB number rule (float value or float token)."""


@dataclass
class InputDigestResult:
    """Structured result from :func:`verify_input_digest`.

    Truthy when ``ok`` is True, falsy otherwise — drop-in replacement for the
    old ``bool`` return in boolean contexts (``if result:``, ``assert result``).
    Identity checks (``is True`` / ``is False``) will not work; use ``.ok``.
    """

    ok: bool
    reason: VerifyReason

    def __bool__(self) -> bool:
        return self.ok


def _has_float_tokens(obj: Any) -> bool:
    """Return True if *obj* (from json.loads) contains any float values.

    Python's json.loads produces ``int`` for integer tokens and ``float`` for
    float tokens, so the presence of a Python ``float`` in the parsed tree is
    a reliable indicator that the original JSON bytes used a float token.
    """
    if isinstance(obj, float):
        return True
    if isinstance(obj, dict):
        return any(_has_float_tokens(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_float_tokens(item) for item in obj)
    return False


def verify_input_digest(
    capsule: dict,
    candidate_input: Any,
    *,
    strict: bool = False,
    raw_json_bytes: bytes | None = None,
) -> InputDigestResult:
    """Return a structured verdict for whether *candidate_input* matches the
    sealed ``agent_input_digest`` in *capsule*.

    **Lenient tier (default):** value-rule normative — enforces that the
    candidate contains no raw floats (``FloatInDigestError`` → NON_CONFORMING)
    and that the JCS-SHA256 digest matches what was sealed (DIGEST_MISMATCH on
    mismatch, VERIFIED on match).

    **Strict tier (opt-in):** additionally checks that *raw_json_bytes*, if
    supplied, contains no float *tokens*.  A producer MUST use integer token
    form, but a lenient verifier cannot detect this from a parsed Python dict
    (declared gap — see module docstring).  Example: a capsule sealed with
    ``{"amount": 42}`` (int token) passes lenient if the caller provides
    ``candidate_input={"amount": 42}`` (Python int), but fails strict if
    ``raw_json_bytes=b'{"amount": 42.0}'`` (float token, same value).

    **Never raises.** Per the profile's structured-result contract, a verifier
    MUST return a result, never propagate an exception.

    Args:
        capsule:         The emitted capsule dict.
        candidate_input: The plaintext candidate to check against the digest.
        strict:          Enable the strict token-form tier (requires
                         *raw_json_bytes*; silently skipped if bytes absent).
        raw_json_bytes:  Original JSON bytes of the candidate input, used only
                         when *strict=True*.

    Returns:
        :class:`InputDigestResult` with ``.ok`` and ``.reason``.
    """
    from agent_action_capsule.canonical import FloatInDigestError, json_digest

    # Strict tier: token-form check from raw bytes.
    if strict and raw_json_bytes is not None:
        try:
            parsed_raw = json.loads(raw_json_bytes)
        except (json.JSONDecodeError, ValueError):
            return InputDigestResult(ok=False, reason=VerifyReason.NON_CONFORMING)
        if _has_float_tokens(parsed_raw):
            return InputDigestResult(ok=False, reason=VerifyReason.NON_CONFORMING)

    # Lenient tier: value-rule check + digest comparison.
    stored = (
        capsule.get("model_attestation", {})
               .get("compute_attestation", {})
               .get("agent_input_digest")
    )
    if stored is None:
        return InputDigestResult(ok=False, reason=VerifyReason.DIGEST_MISMATCH)

    try:
        actual = json_digest(candidate_input)
    except (FloatInDigestError, TypeError, ValueError):
        return InputDigestResult(ok=False, reason=VerifyReason.NON_CONFORMING)

    if stored == actual:
        return InputDigestResult(ok=True, reason=VerifyReason.VERIFIED)
    return InputDigestResult(ok=False, reason=VerifyReason.DIGEST_MISMATCH)
