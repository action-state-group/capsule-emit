# SPDX-License-Identifier: Apache-2.0
"""Illustrative constraint implementations for capsule-emit.

These are example constraints that demonstrate the Constraint protocol.
They are deterministic, model-free, and have no side effects.

For production use, implement your own Constraint classes following the
same protocol.
"""
from capsule_emit.constraints.apache import AmountUnderCap, VendorKnown

__all__ = ["AmountUnderCap", "VendorKnown"]
