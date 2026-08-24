# SPDX-License-Identifier: Apache-2.0
"""Seal-time float rejection must be universal, not money-field-specific.

O16 audit item 15 ("Float rejection EVERYWHERE, not just money"): verify-side
strict-tier checking (``verify.py``'s ``_has_float_tokens``) is confirmed
field-name-agnostic, with real non-money test coverage
(``test_hybrid_verifier.py``). Seal-time rejection delegates to the external
``agent_action_capsule.canonical`` package — every seal-time test that
existed before this file used only money-shaped fields (``amount``,
``total``, ``unit_price_usd``), so the external package's actual recursion
behavior was unverified from this repo for non-money floats (duration,
count, coordinate).

These tests call the real public ``seal()`` entrypoint (not ``json_digest``
directly) with non-money float fields — top-level, nested, in a list, and in
``agent_output`` — mirroring the nested/list coverage that already exists on
the verify side. A failure here means seal-time rejection is narrower than
verify-side rejection: floats could pass into a sealed capsule in a
non-money field that ``verify(strict=True)`` would then have to catch after
the fact, instead of being rejected at the point of authorship.
"""
from __future__ import annotations

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit import seal

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# Negative (mutant): non-money floats must raise, exactly like money floats do
# ---------------------------------------------------------------------------

_NONMONEY_FLOAT_PAYLOADS = [
    ({"duration_seconds": 12.5}, "top-level-duration"),
    ({"retry_count": 3.0}, "top-level-count"),
    ({"latitude": 37.7749}, "top-level-coordinate"),
    ({"telemetry": {"latitude": 37.7749, "longitude": -122.4194}}, "nested-coordinate"),
    ({"job": {"duration_seconds": 12.5}}, "nested-duration"),
    ({"samples": [1, 2.5, 3]}, "list-duration-like"),
    ({"waypoints": [{"lat": 1.0}, {"lat": 2.5}]}, "list-of-dicts-coordinate"),
]


@pytest.mark.parametrize("payload,label", _NONMONEY_FLOAT_PAYLOADS, ids=[x[1] for x in _NONMONEY_FLOAT_PAYLOADS])
def test_seal_rejects_nonmoney_float_in_agent_input(payload, label, tmp_ledger):
    """A non-money float anywhere in agent_input must raise at seal() time."""
    with pytest.raises(FloatInDigestError):
        seal(payload, action="act", operator="org", developer="agent@v1", anchor=False, ledger=tmp_ledger)


def test_seal_rejects_nonmoney_float_in_agent_output(tmp_ledger):
    """The same rejection applies to agent_output, not just agent_input."""
    with pytest.raises(FloatInDigestError):
        seal(
            None,
            action="act",
            operator="org",
            developer="agent@v1",
            agent_output={"elapsed_ms": 42.0},
            anchor=False,
            ledger=tmp_ledger,
        )


# ---------------------------------------------------------------------------
# Positive: the string-encoded equivalents digest and verify cleanly
# ---------------------------------------------------------------------------


def test_seal_accepts_nonmoney_values_as_strings(tmp_ledger):
    """Same shapes as the mutants above, with floats encoded as exact strings."""
    from agent_action_capsule import verify

    cap = seal(
        {
            "duration_seconds": "12.5",
            "retry_count": "3",
            "telemetry": {"latitude": "37.7749", "longitude": "-122.4194"},
            "samples": [1, "2.5", 3],
        },
        action="act",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(cap.capsule).ok
