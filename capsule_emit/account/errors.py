# SPDX-License-Identifier: Apache-2.0
"""Named-reason errors for the neutral account/fold core.

Every rejection carries a stable ``reason`` code so a conformance vector can
pin the reason string, not just "it raised something". These mirror the
capsule-ledger ``folds/errors.py`` convention (named failure reasons) so a
cross-repo vector reads the same reason on either side of the core.
"""
from __future__ import annotations

# Definition-document (canonical-form) reasons.
MALFORMED_DEFINITION = "malformed_definition"
UNKNOWN_DERIVATION_CLASS = "unknown_derivation_class"
UNKNOWN_SELECTION_KIND = "unknown_selection_kind"
EMPTY_READS = "empty_reads"
DUPLICATE_READ_PATH = "duplicate_read_path"
UNDECLARED_PREDICATE_FIELD = "undeclared_predicate_field"
UNBOUNDED_PREDICATE_OP = "unbounded_predicate_operation"
FLOAT_IN_DEFINITION = "float_in_definition"
UNSAFE_INTEGER_IN_DEFINITION = "unsafe_integer_in_definition"

# Account-construction reasons.
MISSING_PROVENANCE = "model_assisted_account_missing_provenance"
PROVENANCE_ON_DETERMINISTIC = "provenance_on_deterministic_account"
MISSING_DERIVATION_REFERENCE = "missing_derivation_reference"
AMBIGUOUS_DERIVATION_REFERENCE = "ambiguous_derivation_reference"
MALFORMED_SELECTION = "malformed_selection"
MALFORMED_COVERAGE = "malformed_coverage"
PER_MEMBER_DIGEST_ON_RANGE = "per_member_digest_on_range_selection"

# Replay/verify reasons.
DERIVATION_CLASS_MISMATCH = "derivation_class_mismatch"
DEFINITION_DIGEST_MISMATCH = "definition_digest_mismatch"
RECOMPUTE_REQUIRED_DETERMINISTIC = "recompute_required_for_deterministic"
RESULT_MISMATCH = "asserted_result_mismatch"
NOT_RE_ADJUDICABLE = "model_assisted_not_re_adjudicable"


class AccountDefinitionError(ValueError):
    """A definition document fails to validate. Carries a named ``reason``."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")


class AccountConstructionError(ValueError):
    """An account object cannot be constructed as asserted. Carries a named ``reason``.

    The canonical case is a ``model_assisted`` account with no provenance: the
    core REFUSES it at construction rather than minting a provenance-free
    model-assisted claim.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")


class AccountVerificationError(RuntimeError):
    """An account fails replay/verify. Carries a named ``reason``."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")
