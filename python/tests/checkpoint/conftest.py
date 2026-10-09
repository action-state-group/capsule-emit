# SPDX-License-Identifier: Apache-2.0
"""A minimal, dependency-free ``LogSource`` fake for testing ``MmrLedger``.

capsule-emit carries no dependency on capsule-ledger's real ``LedgerStore``
(that would defeat the point of the port -- see the ``checkpoint`` package
docstring). This fake satisfies the same structural shape
(``append``/``scan``/``fetch``/``find_gaps``/``verify``, records exposing
``.seq``/``.capsule_id``) so ``MmrLedger`` is exercised exactly as it will be
by any real log binding, capsule-ledger's included.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import pytest


@dataclass
class FakeRecord:
    seq: int
    capsule_id: str
    capsule: dict


@dataclass
class FakeLogSource:
    """In-memory, gapless-seq log satisfying the ``LogSource`` shape."""

    records: list[FakeRecord] = field(default_factory=list)

    def append(self, capsule: dict, *, consequential: bool = True) -> FakeRecord:
        seq = len(self.records) + 1
        capsule_id = capsule.get("capsule_id") or hashlib.sha256(
            json.dumps(capsule, sort_keys=True, default=str).encode()
        ).hexdigest()
        rec = FakeRecord(seq=seq, capsule_id=capsule_id, capsule=dict(capsule))
        self.records.append(rec)
        return rec

    def scan(self, query=None):
        return iter(self.records)

    def fetch(self, capsule_id: str) -> FakeRecord | None:
        for r in self.records:
            if r.capsule_id == capsule_id:
                return r
        return None

    def verify(self, capsule_id: str) -> None:
        return None

    def find_gaps(self) -> list:
        return []


def synthetic_capsule(i: int) -> dict:
    return {
        "operator": "acme",
        "developer": "agent-x",
        "action_type": "decide",
        "timestamp": f"2026-01-01T00:00:{i:02d}Z",
        "disposition": {"verdict_class": "executed"},
        "seq_marker": i,
    }


@pytest.fixture
def log_source() -> FakeLogSource:
    return FakeLogSource()
