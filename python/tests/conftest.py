# SPDX-License-Identifier: Apache-2.0
"""Suite-wide test setup.

Most tests exercise flat-ledger behaviour, which stays supported for existing
ledgers, by sealing into a fresh ``ledger.jsonl``. Since capsule-emit#358 no
new flat ledger is created and writing one warns, so this lets the suite
create them; ``tests/test_ledger_layout.py`` turns it off to test the rule
itself, and checks the warning explicitly.
"""
from __future__ import annotations

import pytest

from capsule_emit import ledger_layout


@pytest.fixture(autouse=True)
def _flat_ledgers_for_the_suite(monkeypatch):
    monkeypatch.setattr(ledger_layout, "_allow_new_flat", True)
    monkeypatch.setattr(ledger_layout, "_warn_flat_writes", False)
