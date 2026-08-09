# SPDX-License-Identifier: Apache-2.0
"""Named-reason errors for hold lifecycle operations — every rejection
carries a stable ``reason_code`` so a caller/test can pin the reason, not
just "it denied something"."""
from __future__ import annotations

__all__ = [
    "FLOAT_IN_HOLD_AMOUNT",
    "NON_INTEGER_HOLD_AMOUNT",
    "HOLD_NOT_FOUND",
    "HOLD_ALREADY_TERMINAL",
    "HOLD_STATUS_AMBIGUOUS",
    "RECONCILE_AFTER_EXPIRY",
    "OVER_TOLERANCE",
    "CAP_EXCEEDED",
    "SEQUENCER_UNAVAILABLE",
    "HoldError",
]

# Record-build-time numeric discipline: amounts are integer minor units,
# floats MUST-FAIL (mirrors the JCS digest's own float intolerance).
FLOAT_IN_HOLD_AMOUNT = "float_in_hold_amount"
NON_INTEGER_HOLD_AMOUNT = "non_integer_hold_amount"

# Hold-lifecycle (release/expire/reconcile) reasons.
HOLD_NOT_FOUND = "hold_not_found"
HOLD_ALREADY_TERMINAL = "hold_already_terminal"
HOLD_STATUS_AMBIGUOUS = "hold_status_ambiguous"
RECONCILE_AFTER_EXPIRY = "reconcile_after_expiry_denied"
OVER_TOLERANCE = "reconcile_over_tolerance"

# evaluate-and-reserve reasons.
CAP_EXCEEDED = "cap_exceeded"
SEQUENCER_UNAVAILABLE = "sequencer_unavailable"


class HoldError(ValueError):
    """A hold record fails to build, or a hold-lifecycle operation is asked
    to do something it structurally cannot. Carries a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")
