# SPDX-License-Identifier: Apache-2.0
"""Times this package commits to (a disclosure record's ``timestamp``, a
signed refusal's ``issued_at``, a hold action's ``timestamp``) are
whole-second UTC with no fraction, the one RFC 3339 form every checkpoint
verifier accepts."""
from __future__ import annotations

import pytest
from _timestamp_rule import is_whole_second


@pytest.mark.parametrize(
    "module, helper",
    [
        ("capsule_emit.disclose", "_now_iso"),
        ("capsule_emit.evidence_request", "_now_iso"),
        ("capsule_emit.holds.action", "_utc_now"),
    ],
)
def test_committed_times_are_whole_seconds(module, helper):
    import importlib

    now = getattr(importlib.import_module(module), helper)()
    assert is_whole_second(now), f"{module}.{helper}() -> {now}"
