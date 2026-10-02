# SPDX-License-Identifier: Apache-2.0
"""The TypeScript CLL verifier's timestamp rule, ported for tests that hold
this package's committed times to it."""
from __future__ import annotations

import re
from datetime import datetime

WHOLE_SECOND = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def cll_ts_accepts_time(t: str) -> bool:
    """``formatTime(t) === t``: an RFC 3339 UTC time ending in ``Z`` whose
    fraction, if any, has no trailing zero."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z", t)
    if match is None or (match.group(2) or "").endswith("0"):
        return False
    try:
        datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def is_whole_second(t: str) -> bool:
    """Whole-second UTC with no fraction (``2026-10-01T23:04:00Z``), the form
    this package commits."""
    return WHOLE_SECOND.fullmatch(t) is not None and cll_ts_accepts_time(t)
