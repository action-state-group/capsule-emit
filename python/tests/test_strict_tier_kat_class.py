# SPDX-License-Identifier: Apache-2.0
"""Strict-tier KAT class: 1e2 vs 100, duplicate keys, -0, 01.

[strict-tier-kat-class]: the strict verifier tier gets its own KAT class
covering four raw-token ambiguities that value-based parsing collapses —
float token form (``1e2``), duplicate object keys, negative zero (``-0``),
and non-canonical leading-zero integers (``01``) — mirroring
scitt-payload-binding's ``lib/cpb/check.py`` R rule and its
``vectors/cpb-check`` KAT vectors.

Load-bearing constraint, tested directly here: the strict tier is
accept/reject only. It MUST NOT alter, substitute, or recompute what gets
digested. A strict-tier run over a conforming payload MUST produce a
byte-identical digest to a lenient (value-rule) run over the same payload,
and a strict-tier rejection MUST occur before ``json_digest`` is ever
invoked — proved below by a mutant test that patches ``json_digest`` and
asserts it is never called for a KAT-non-conforming raw payload.
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from capsule_emit import VerifyReason, seal, verify_input_digest


def _tmp_ledger() -> str:
    return os.path.join(tempfile.mkdtemp(), "ledger.jsonl")


def _emit_with(agent_input: dict) -> dict:
    r = seal(
        agent_input,
        action="test_action",
        operator="op",
        developer="dev",
        anchor=False,
        ledger=_tmp_ledger(),
    )
    return r.capsule


# ---------------------------------------------------------------------------
# KAT class: each of the four raw-token ambiguities is rejected by the
# strict tier even though the parsed value passes lenient.
# ---------------------------------------------------------------------------


def test_kat_exponent_notation_1e2_vs_100() -> None:
    """`1e2` and `100` parse to the same value; strict tier rejects the token."""
    capsule = _emit_with({"amount": 100})
    raw = b'{"amount": 1e2}'
    strict = verify_input_digest(capsule, {"amount": 100}, strict=True, raw_json_bytes=raw)
    lenient = verify_input_digest(capsule, {"amount": 100})
    assert lenient.ok
    assert not strict.ok
    assert strict.reason == VerifyReason.NON_CONFORMING


def test_kat_duplicate_key_top_level() -> None:
    """A duplicate key is silently collapsed by json.loads (last wins); strict rejects it."""
    capsule = _emit_with({"amount": 2})
    raw = b'{"amount": 1, "amount": 2}'
    strict = verify_input_digest(capsule, {"amount": 2}, strict=True, raw_json_bytes=raw)
    lenient = verify_input_digest(capsule, {"amount": 2})
    assert lenient.ok
    assert not strict.ok
    assert strict.reason == VerifyReason.NON_CONFORMING


def test_kat_duplicate_key_nested() -> None:
    """Duplicate-key detection applies at every depth, not just top level."""
    capsule = _emit_with({"nested": {"val": 2}})
    raw = b'{"nested": {"val": 1, "val": 2}}'
    strict = verify_input_digest(
        capsule, {"nested": {"val": 2}}, strict=True, raw_json_bytes=raw
    )
    lenient = verify_input_digest(capsule, {"nested": {"val": 2}})
    assert lenient.ok
    assert not strict.ok
    assert strict.reason == VerifyReason.NON_CONFORMING


def test_kat_negative_zero() -> None:
    """`-0` parses to Python int 0, indistinguishable from `0`; strict rejects the token."""
    capsule = _emit_with({"amount": 0})
    raw = b'{"amount": -0}'
    strict = verify_input_digest(capsule, {"amount": 0}, strict=True, raw_json_bytes=raw)
    lenient = verify_input_digest(capsule, {"amount": 0})
    assert lenient.ok
    assert not strict.ok
    assert strict.reason == VerifyReason.NON_CONFORMING


def test_kat_leading_zero() -> None:
    """`01` is not a valid canonical integer token; strict tier rejects it fail-closed."""
    capsule = _emit_with({"amount": 1})
    raw = b'{"amount": 01}'
    strict = verify_input_digest(capsule, {"amount": 1}, strict=True, raw_json_bytes=raw)
    assert not strict.ok
    assert strict.reason == VerifyReason.NON_CONFORMING


def test_kat_canonical_tokens_pass_strict() -> None:
    """None of the four KAT categories false-positive on a conforming payload."""
    capsule = _emit_with({"a": 1, "b": -5, "c": 0, "nested": {"d": 3}})
    raw = b'{"a": 1, "b": -5, "c": 0, "nested": {"d": 3}}'
    strict = verify_input_digest(
        capsule,
        {"a": 1, "b": -5, "c": 0, "nested": {"d": 3}},
        strict=True,
        raw_json_bytes=raw,
    )
    assert strict.ok
    assert strict.reason == VerifyReason.VERIFIED


# ---------------------------------------------------------------------------
# Load-bearing: the strict tier never moves the digest — accept/reject only.
# ---------------------------------------------------------------------------


def test_strict_accept_digest_identical_to_lenient() -> None:
    """A candidate the strict tier accepts digests to exactly what lenient produces."""
    capsule = _emit_with({"amount": 42, "label": "ok"})
    candidate = {"amount": 42, "label": "ok"}
    raw = b'{"amount": 42, "label": "ok"}'

    lenient = verify_input_digest(capsule, candidate)
    strict = verify_input_digest(capsule, candidate, strict=True, raw_json_bytes=raw)

    assert lenient.ok and strict.ok
    assert lenient.reason == strict.reason == VerifyReason.VERIFIED
    # Both compared against the same sealed digest with the same candidate:
    # if the strict tier had recomputed or substituted anything before
    # comparison, one of these two would have flipped independently of the
    # other. They cannot, because both paths call json_digest on the same
    # untouched `candidate_input` object (see test below for direct proof).


def test_strict_rejection_never_reaches_json_digest() -> None:
    """Mutant guard: json_digest must never be called when the strict tier rejects.

    This is the direct proof of "accept/reject only": if a future change
    moved the strict-tier check to run *after* digest computation (or made
    it recompute/substitute a value before digesting), this test would start
    calling the patched ``json_digest`` and fail.
    """
    capsule = _emit_with({"amount": 100})
    kat_violations = [
        b'{"amount": 1e2}',              # exponent notation
        b'{"amount": 2, "amount": 100}',  # duplicate key
        b'{"amount": -0}',                # negative zero
        b'{"amount": 01}',                # leading zero
    ]
    with patch("agent_action_capsule.canonical.json_digest") as mock_digest:
        for raw in kat_violations:
            result = verify_input_digest(
                capsule, {"amount": 100}, strict=True, raw_json_bytes=raw
            )
            assert result.reason == VerifyReason.NON_CONFORMING, raw
        mock_digest.assert_not_called()


def test_strict_accept_calls_json_digest_exactly_once_on_untouched_candidate() -> None:
    """When the strict tier accepts, json_digest runs exactly once, on `candidate_input`
    unchanged — never on a value derived from the raw bytes."""
    capsule = _emit_with({"amount": 42})
    candidate = {"amount": 42}
    raw = b'{"amount": 42}'

    from agent_action_capsule.canonical import json_digest as real_json_digest

    with patch(
        "agent_action_capsule.canonical.json_digest", wraps=real_json_digest
    ) as mock_digest:
        result = verify_input_digest(capsule, candidate, strict=True, raw_json_bytes=raw)

    assert result.ok
    mock_digest.assert_called_once_with(candidate)
