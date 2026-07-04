# SPDX-License-Identifier: Apache-2.0
"""nanda_tax_audit — "cook the books, get caught" NANDA Town scenario."""

from .scenario import CAPSULE_LEDGER, tax_audit_factory

__all__ = ["tax_audit_factory", "CAPSULE_LEDGER"]
