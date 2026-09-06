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
    inspects the raw JSON bytes for the strict-tier KAT class — float tokens
    (e.g. ``42.0``, ``1e2``), non-canonical integer tokens (``-0``, ``01``),
    and duplicate object keys. A producer MUST use canonical integer token
    form (``0|-?[1-9][0-9]*``) and unique keys; a lenient verifier that
    receives only a parsed Python dict cannot enforce this (declared gap —
    JSON parsing collapses ``42.0``/``42`` to the same value, ``-0``/``0`` to
    the same int, and silently keeps only the last of a duplicate key). The
    strict tier closes that gap by requiring the caller to supply the
    original JSON bytes alongside the parsed value.

    **Load-bearing constraint: accept/reject only.** The strict tier never
    computes, substitutes, or otherwise moves the digest — it is a pure
    pre-check that runs, and can only reject, before ``json_digest`` is ever
    invoked (see the strict-tier block in :func:`verify_input_digest`). A
    candidate accepted by the strict tier therefore digests to the exact same
    value the lenient tier would have produced for it: strict verification
    can only narrow which candidates reach digest comparison, never change
    what gets digested. See scitt-payload-binding's ``lib/cpb/check.py`` R
    rule and its ``vectors/cpb-check`` KAT vectors for the same class defined
    independently of this implementation.

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
import re
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


_STRICT_INT_TOKEN_RE = re.compile(r"^(?:0|-?[1-9][0-9]*)$")


class _StrictTokenViolation(Exception):
    """Internal signal: a raw JSON token failed a strict-tier KAT rule."""


def _strict_parse_int(token: str) -> int:
    """``json.loads`` int hook: reject any non-canonical integer token.

    Called with the exact matched token text (e.g. ``"-0"``, ``"01"``), which
    is the only place this distinction survives — after parsing, ``-0`` and
    ``01`` are indistinguishable from ``0`` and ``1``.
    """
    if not _STRICT_INT_TOKEN_RE.match(token):
        raise _StrictTokenViolation(f"non-canonical integer token {token!r}")
    return int(token)


def _strict_parse_float(token: str) -> float:
    """``json.loads`` float hook: any float token is a strict-tier violation."""
    raise _StrictTokenViolation(f"float token {token!r}")


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` object_pairs hook: reject duplicate keys at any depth.

    ``json.loads`` normally keeps only the last of a duplicate key; this hook
    fires per-object (including nested objects) before that collapse.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _StrictTokenViolation(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _has_strict_token_violations(raw_json_bytes: bytes) -> bool:
    """Return True if *raw_json_bytes* violates the strict-tier KAT class.

    Covers, at the raw-token level (before value coercion collapses the
    distinction): float tokens (``1e2``, ``42.0``), non-canonical integer
    tokens (``-0``, ``01``), and duplicate object keys — the same class
    scitt-payload-binding's ``lib/cpb/check.py`` R rule defines against its
    ``vectors/cpb-check`` KAT vectors. Malformed JSON also returns True.
    """
    try:
        json.loads(
            raw_json_bytes,
            parse_int=_strict_parse_int,
            parse_float=_strict_parse_float,
            object_pairs_hook=_strict_object_pairs,
        )
    except _StrictTokenViolation:
        return True
    except (json.JSONDecodeError, ValueError):
        return True
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
    supplied, contains no strict-tier KAT-class violation: float tokens,
    non-canonical integer tokens (``-0``, ``01``), or duplicate object keys.
    A producer MUST use canonical integer token form and unique keys, but a
    lenient verifier cannot detect a token-level violation from a parsed
    Python dict (declared gap — see module docstring).  Example: a capsule
    sealed with ``{"amount": 42}`` (int token) passes lenient if the caller
    provides ``candidate_input={"amount": 42}`` (Python int), but fails
    strict if ``raw_json_bytes=b'{"amount": 42.0}'`` (float token, same
    value). Accept/reject only: this tier runs, and can only reject, before
    the digest below is computed, so a candidate it accepts always digests
    identically to a lenient-only run.

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

    # Strict tier: token-form check from raw bytes. Accept/reject only — this
    # block returns before json_digest is ever called below, so it can never
    # alter what gets digested (see module docstring, "accept/reject only").
    if strict and raw_json_bytes is not None:
        if _has_strict_token_violations(raw_json_bytes):
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
