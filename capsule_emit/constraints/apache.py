# SPDX-License-Identifier: Apache-2.0
"""Illustrative Apache-licensed constraint implementations.

These are toy constraints that demonstrate the Constraint protocol.
They are:
- Deterministic (same inputs always produce the same result)
- Model-free (no LLM calls, no I/O)
- Stateless per-call (safe to run in any order, any thread)

For real deployments, implement constraints that match your domain's
actual policy rules.  These examples show the interface shape only.
"""
from __future__ import annotations

from typing import Any

__all__ = ["AmountUnderCap", "VendorKnown"]


class AmountUnderCap:
    """Illustrative constraint: the ``amount`` input must be below ``cap``.

    This is an example of a simple threshold constraint.  The check is
    purely arithmetic — no model call, no external lookup.

    Args:
        cap: The exclusive upper bound on the ``amount`` input.

    Example::

        c = AmountUnderCap(5000)
        assert c.check({"amount": 1200}, None) == (True, None)
        ok, reason = c.check({"amount": 9999}, None)
        assert not ok
    """

    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.name = f"amount_under_{cap}"

    def check(self, inputs: dict, output: Any) -> tuple[bool, str | None]:
        """Return ``(True, None)`` if ``inputs["amount"] < cap``."""
        amount = inputs.get("amount", 0)
        if amount < self.cap:
            return (True, None)
        return (False, f"amount {amount} >= cap {self.cap}")


class VendorKnown:
    """Illustrative constraint: the ``vendor`` input must be in a known set.

    This is an example of a simple allowlist constraint.  The lookup is
    a set membership test — no model call, no external lookup.

    Args:
        known: The set of approved vendor names.

    Example::

        c = VendorKnown({"Acme", "Globex"})
        assert c.check({"vendor": "Acme"}, None) == (True, None)
        ok, reason = c.check({"vendor": "EvilCorp"}, None)
        assert not ok
    """

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.name = "vendor_known"

    def check(self, inputs: dict, output: Any) -> tuple[bool, str | None]:
        """Return ``(True, None)`` if ``inputs["vendor"]`` is in the known set."""
        vendor = inputs.get("vendor")
        if vendor in self.known:
            return (True, None)
        return (False, f"vendor '{vendor}' not in approved set")
