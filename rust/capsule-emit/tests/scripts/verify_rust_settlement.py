#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Settlement-records oracle: derive the states of Rust-sealed legs with the
Python reference verifier, independent of the Rust code that sealed them.

Usage:
    verify_rust_settlement.py <legs.json>

Prints one JSON line: {"conforming", "failures", "findings", "diagnostics",
"settlements": [{"payment_state", "delivery_state", ...}]}. Exit 0 iff
conforming.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from capsule_emit.settlement import verify_settlements


def main() -> int:
    legs = json.loads(Path(sys.argv[1]).read_text())
    report = verify_settlements(legs)
    print(json.dumps({"conforming": report.conforming, "failures": report.failures,
                      "findings": report.findings, "diagnostics": report.diagnostics,
                      "settlements": report.settlements}))
    return 0 if report.conforming else 1


if __name__ == "__main__":
    raise SystemExit(main())
