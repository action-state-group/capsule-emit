# SPDX-License-Identifier: Apache-2.0
"""``seal(salt_digests=True)``: per-emit random salt on digest-committed fields.

Opt-in (default ``False``) so existing deterministic-digest callers and tests
are unaffected. When enabled, ``agent_input_digest`` / ``agent_output_digest``
fold in a random salt, stored as ``digest_salt`` in ``compute_attestation`` so
the emitting operator can recompute and verify their own capsule — but an
outside observer without the salt cannot correlate two capsules carrying the
same logical input via digest equality.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from agent_action_capsule.canonical import jcs, normalize

import capsule_emit


def _emit_and_read(receipt: dict, *, salt_digests: bool) -> dict:
    with tempfile.TemporaryDirectory() as d:
        ledger = str(Path(d) / "ledger.jsonl")
        capsule_emit.seal(
            receipt,
            action="purchase",
            operator="did:key:zOperator",
            anchor=False,
            ledger=ledger,
            salt_digests=salt_digests,
        )
        return capsule_emit.read_ledger(ledger)[0]


def test_unsalted_by_default_no_salt_field():
    capsule = _emit_and_read({"amount": "10.00"}, salt_digests=False)
    assert "digest_salt" not in capsule["model_attestation"]["compute_attestation"]


def test_salted_digest_differs_across_two_calls_with_identical_input():
    receipt = {"amount": "10.00", "vendor": "acme"}
    c1 = _emit_and_read(receipt, salt_digests=True)
    c2 = _emit_and_read(receipt, salt_digests=True)
    d1 = c1["model_attestation"]["compute_attestation"]["agent_input_digest"]
    d2 = c2["model_attestation"]["compute_attestation"]["agent_input_digest"]
    assert d1 != d2, "same logical input must not produce the same digest across calls"


def test_salted_digest_recomputes_from_the_stored_salt():
    """The whole point of storing digest_salt: the emitting operator can
    always recompute and verify their own capsule."""
    receipt = {"amount": "10.00", "vendor": "acme"}
    capsule = _emit_and_read(receipt, salt_digests=True)
    att = capsule["model_attestation"]["compute_attestation"]
    salt = att["digest_salt"]
    recomputed = hashlib.sha256(jcs(normalize(receipt)) + b"|" + salt.encode("utf-8")).hexdigest()
    assert recomputed == att["agent_input_digest"]


def test_salted_digest_still_diverges_for_actually_different_input():
    c1 = _emit_and_read({"amount": "10.00"}, salt_digests=True)
    c2 = _emit_and_read({"amount": "20.00"}, salt_digests=True)
    assert (
        c1["model_attestation"]["compute_attestation"]["agent_input_digest"]
        != c2["model_attestation"]["compute_attestation"]["agent_input_digest"]
    )


def test_confirmed_effect_response_digest_uses_the_same_salt_as_agent_output():
    """response_digest is auto-derived from agent_output on a confirmed
    effect — it must be salted with the SAME salt so it matches
    agent_output_digest, not a second, independently-salted digest."""
    with tempfile.TemporaryDirectory() as d:
        ledger = str(Path(d) / "ledger.jsonl")
        capsule_emit.seal(
            None,
            action="purchase",
            operator="did:key:zOperator",
            agent_output={"status": "shipped"},
            effect={"type": "purchase", "status": "confirmed"},
            anchor=False,
            ledger=ledger,
            salt_digests=True,
        )
        capsule = capsule_emit.read_ledger(ledger)[0]
    att = capsule["model_attestation"]["compute_attestation"]
    assert att["agent_output_digest"] == capsule["effect"]["response_digest"]
