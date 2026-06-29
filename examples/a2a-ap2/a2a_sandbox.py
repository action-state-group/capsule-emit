# SPDX-License-Identifier: Apache-2.0
"""Minimal A2A + AP2 sandbox — mirrors real A2A Task and AP2 CartMandate JSON shapes.

This module does NOT implement A2A wire protocol. It provides:
- Data classes that match the A2A Task JSON shape (so the example reads as real)
- An AP2 CartMandate structure (A2A Payment Profile v2)
- A deterministic sandbox payment executor (no Stripe key needed)

To use real Stripe: set STRIPE_API_KEY and DRY_RUN will be False.
The capsule logic is identical in both modes — only the payment executor differs.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# AP2 types (subset of A2A Payment Profile v2)
# ---------------------------------------------------------------------------

@dataclass
class Money:
    value: str      # exact decimal string, e.g. "250.00"
    currency: str   # ISO 4217, e.g. "USD"

    def as_dict(self) -> dict:
        return {"value": self.value, "currency": self.currency}


@dataclass
class AP2CartMandate:
    """The 'may': caller authorizes callee to pay up to max_amount for this cart."""
    mandate_id: str
    payee_name: str
    payee_id: str
    max_amount: Money
    cart_ref: str           # invoice / PO number
    authorized_by: str      # policy id or authorizer identity
    expires_at: str         # RFC 3339 UTC

    def as_dict(self) -> dict:
        return {
            "type": "ap2:CartMandate",
            "mandate_id": self.mandate_id,
            "payee": {"name": self.payee_name, "id": self.payee_id},
            "max_amount": self.max_amount.as_dict(),
            "cart_ref": self.cart_ref,
            "authorized_by": self.authorized_by,
            "expires_at": self.expires_at,
        }


@dataclass
class A2ATask:
    """Minimal A2A Task envelope carrying an AP2 CartMandate."""
    task_id: str
    session_id: str
    mandate: AP2CartMandate
    agent_card_url: str = "https://agent.example/.well-known/agent.json"

    def input_dict(self) -> dict:
        """The agent_input to digest: the full A2A Task input."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_card_url": self.agent_card_url,
            "input": self.mandate.as_dict(),
        }


# ---------------------------------------------------------------------------
# Payment result (the 'did')
# ---------------------------------------------------------------------------

@dataclass
class PaymentResult:
    payment_id: str
    amount: Money
    payee_name: str
    payee_id: str
    cart_ref: str
    status: str             # "completed" | "failed" | "declined"
    error: str | None = None

    def as_dict(self) -> dict:
        d: dict[str, Any] = {
            "payment_id": self.payment_id,
            "amount": self.amount.as_dict(),
            "payee": {"name": self.payee_name, "id": self.payee_id},
            "cart_ref": self.cart_ref,
            "status": self.status,
        }
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------
# Sandbox payment executor
# ---------------------------------------------------------------------------

_DRY_RUN = os.environ.get("DRY_RUN", "1").lower() not in ("0", "false", "no")
_STRIPE_KEY = os.environ.get("STRIPE_API_KEY", "")
_USE_REAL_STRIPE = bool(_STRIPE_KEY) and not _DRY_RUN


def execute_payment(mandate: AP2CartMandate, amount: Money) -> PaymentResult:
    """Execute (or simulate) the payment authorized by the mandate.

    Sandbox mode (DRY_RUN=1, default): returns a deterministic result that
    mirrors the real Stripe shape — no network call, no Stripe key needed.

    Real mode: requires STRIPE_API_KEY and DRY_RUN != 1.
    """
    if _USE_REAL_STRIPE:
        return _real_payment(mandate, amount)
    return _sandbox_payment(mandate, amount)


def _sandbox_payment(mandate: AP2CartMandate, amount: Money) -> PaymentResult:
    pid = f"pi_sandbox_{int(time.time()):x}"
    return PaymentResult(
        payment_id=pid,
        amount=amount,
        payee_name=mandate.payee_name,
        payee_id=mandate.payee_id,
        cart_ref=mandate.cart_ref,
        status="completed",
    )


def _real_payment(mandate: AP2CartMandate, amount: Money) -> PaymentResult:
    try:
        import stripe  # type: ignore[import]
        stripe.api_key = _STRIPE_KEY
    except ImportError as exc:
        raise RuntimeError("pip install stripe  (or set DRY_RUN=1)") from exc

    cents = int(float(amount.value) * 100)
    pi = stripe.PaymentIntent.create(
        amount=cents,
        currency=amount.currency.lower(),
        description=f"AP2 payment: {mandate.cart_ref} → {mandate.payee_name}",
        confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )
    raw = pi if isinstance(pi, dict) else dict(pi)
    return PaymentResult(
        payment_id=raw.get("id", ""),
        amount=amount,
        payee_name=mandate.payee_name,
        payee_id=mandate.payee_id,
        cart_ref=mandate.cart_ref,
        status="completed" if raw.get("status") == "succeeded" else "failed",
    )


def is_sandbox() -> bool:
    return not _USE_REAL_STRIPE
