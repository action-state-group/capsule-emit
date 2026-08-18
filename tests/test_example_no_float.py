# SPDX-License-Identifier: Apache-2.0
"""Regression: example call sites must not pass floats to digest-bearing fields.

Guards the float fixes in agentgateway-capsule/demo.py, quickstart_demo.py,
goose-capsule/server.py, and wicket/demo.py so that float literals in those
example sites cannot regress silently.

Negative (mutant) checks confirm the guard is load-bearing: each mutant
replaces a fixed value with the unfixed float form and asserts FloatInDigestError
is raised — so a test failure here means the guard has been bypassed.
"""
from __future__ import annotations

import pytest
from agent_action_capsule.canonical import FloatInDigestError, json_digest

# ---------------------------------------------------------------------------
# Positive: every fixed example payload must digest without error
# ---------------------------------------------------------------------------

_FIXED_PAYLOADS = [
    # quickstart_demo.py — agent_input
    ({"vendor": "Frobozz Supply", "total": "1240.19"},         "quickstart-agent-input"),
    # agentgateway-capsule/demo.py — submit_order arguments
    ({"vendor": "Frobozz Supply", "amount": "1240.19", "po_number": "PO-7777"},
                                                               "agentgateway-submit-order-args"),
    # agentgateway-capsule/demo.py — get_price tool_result
    ({"unit_price_usd": "42.00", "currency": "USD"},          "agentgateway-get-price-result"),
    # goose-capsule/server.py — get_price return value
    ({"vendor": "Frobozz", "item": "widget", "unit_price_usd": "42.00", "currency": "USD"},
                                                               "goose-get-price-result"),
    # goose-capsule/server.py — other price entries
    ({"vendor": "X", "item": "gadget", "unit_price_usd": "128.50", "currency": "USD"},
                                                               "goose-gadget-price-result"),
    ({"vendor": "X", "item": "doohickey", "unit_price_usd": "9.99", "currency": "USD"},
                                                               "goose-doohickey-price-result"),
    # wicket/demo.py — @emitter.tool call sites (integers, quantized whole-number amounts)
    ({"vendor": "Globex", "amount": 2500},                    "wicket-mcp-pass"),
    ({"vendor": "Acme",   "amount": 999},                     "wicket-mcp-block"),
    ({"vendor": "Acme",   "amount": 9999},                    "wicket-mcp-no-callback"),
]


@pytest.mark.parametrize("payload,label", _FIXED_PAYLOADS, ids=[x[1] for x in _FIXED_PAYLOADS])
def test_fixed_example_payload_digests_cleanly(payload, label):
    """All fixed example payloads must serialize without FloatInDigestError."""
    json_digest(payload)  # must not raise


# ---------------------------------------------------------------------------
# Negative (mutant): float literals in the same fields must raise
# ---------------------------------------------------------------------------

_FLOAT_MUTANTS = [
    # quickstart_demo.py — unfixed form
    ({"vendor": "Frobozz Supply", "total": 1240.19},           "quickstart-total-float"),
    # agentgateway-capsule/demo.py — unfixed submit_order amount
    ({"vendor": "Frobozz Supply", "amount": 1240.19, "po_number": "PO-7777"},
                                                               "agentgateway-amount-float"),
    # agentgateway-capsule/demo.py — unfixed get_price result
    ({"unit_price_usd": 42.00, "currency": "USD"},             "agentgateway-price-float"),
    # goose-capsule/server.py — unfixed price dict value
    ({"vendor": "X", "item": "widget", "unit_price_usd": 42.00, "currency": "USD"},
                                                               "goose-price-float"),
    # wicket/demo.py — unfixed float amount
    ({"vendor": "Globex", "amount": 2500.0},                   "wicket-amount-float"),
]


@pytest.mark.parametrize("payload,label", _FLOAT_MUTANTS, ids=[x[1] for x in _FLOAT_MUTANTS])
def test_float_mutant_in_digest_field_raises(payload, label):
    """A float literal in a digest-bearing field must raise FloatInDigestError.

    These payloads are the unfixed (mutant) forms of the example sites.
    If this test stops failing, the §5.1 guard has been bypassed.
    """
    with pytest.raises(FloatInDigestError):
        json_digest(payload)
