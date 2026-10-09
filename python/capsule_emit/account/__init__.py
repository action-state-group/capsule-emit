# SPDX-License-Identifier: Apache-2.0
"""The neutral account / fold core.

One place for: fold-definition-as-DATA + ``definition_digest``; the account
object {selection(kind, coverage), derivation(definition_digest | registry_token,
class), asserted_result, provenance}; and replay/verify (deterministic →
recompute+match; model_assisted → provenance-verify), idempotent-on-replay.

This is the single implementation of ``AccountDefinition`` / ``definition_digest``
in the neutral stack. Consumers (a ledger's folds layer, a mesh account capsule)
re-import THIS module through the public interface rather than re-forking the
definition/digest/replay contracts — there must be no second ``definition_digest``
implementation anywhere outside this core.

Meter-not-price: an account counts and asserts a result over a selected range or
set; it carries no currency, rate, or price.
"""
from __future__ import annotations

from .account import (
    Account,
    Coverage,
    Derivation,
    Provenance,
    Selection,
    build_account,
)
from .definition import (
    BOUNDED_PREDICATE_OPS,
    DERIVATION_CLASSES,
    DERIVATION_DETERMINISTIC,
    DERIVATION_MODEL_ASSISTED,
    SELECTION_CHAIN_SEGMENT,
    SELECTION_EXPLICIT_SET,
    SELECTION_KINDS,
    SELECTION_RANGE,
    AccountDefinition,
    Predicate,
    parse_definition,
)
from .errors import (
    AccountConstructionError,
    AccountDefinitionError,
    AccountVerificationError,
)
from .replay import (
    RecomputeFn,
    VerificationResult,
    verify_account,
)

__all__ = [
    # definition-as-data
    "AccountDefinition",
    "Predicate",
    "parse_definition",
    # derivation classes / selection kinds
    "DERIVATION_DETERMINISTIC",
    "DERIVATION_MODEL_ASSISTED",
    "DERIVATION_CLASSES",
    "SELECTION_RANGE",
    "SELECTION_EXPLICIT_SET",
    "SELECTION_CHAIN_SEGMENT",
    "SELECTION_KINDS",
    "BOUNDED_PREDICATE_OPS",
    # account object
    "Account",
    "Selection",
    "Coverage",
    "Derivation",
    "Provenance",
    "build_account",
    # replay / verify
    "verify_account",
    "VerificationResult",
    "RecomputeFn",
    # errors
    "AccountDefinitionError",
    "AccountConstructionError",
    "AccountVerificationError",
]
