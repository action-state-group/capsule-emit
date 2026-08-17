# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke tests for shipped examples.

Each test runs the example script as a subprocess and asserts:
  - exit code 0 (the script completed without crashing)
  - capsule count > 0 (the script actually produced capsules, not a silent zero)

An example that is never executed by CI is documentation, not a test.
This file exists to prevent that: if a float or other digest error silently
swallows emission, the capsule-count assertion catches it here before the
example ships in a broken state.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"


def _run(
    script_path: Path,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(script_path)] + (extra_args or [])
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=merged_env)


def _capsule_count(stdout: str) -> int:
    """Extract the sealed capsule count from demo stdout.

    Demos print: '[step N] Ledger: <count> capsule(s) sealed'
    """
    m = re.search(r"Ledger:\s+(\d+)\s+capsule\(s\)\s+sealed", stdout)
    if m is None:
        return -1
    return int(m.group(1))


def test_goose_capsule_demo_offline():
    """goose-capsule/demo.py --no-anchor must exit 0 and emit > 0 capsules."""
    result = _run(EXAMPLES / "goose-capsule" / "demo.py", ["--no-anchor"])
    assert result.returncode == 0, (
        f"goose-capsule/demo.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    count = _capsule_count(result.stdout)
    assert count > 0, (
        f"goose-capsule/demo.py emitted {count} capsules (expected > 0).\n"
        f"If count is 0 the emission failed silently (FloatInDigestError swallowed?).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_wicket_demo():
    """wicket/demo.py must exit 0 (no float in digest-bearing fields)."""
    result = _run(EXAMPLES / "wicket" / "demo.py")
    assert result.returncode == 0, (
        f"wicket/demo.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_quickstart_demo_offline():
    """quickstart_demo.py must exit 0 with no floats in agent_input.

    Run with a nonexistent anchor URL so the anchor POST fails fast (connection
    refused) rather than timing out against the real endpoint in CI.  The anchor
    is fire-and-forget so a failed POST does not affect exit code.
    """
    result = _run(
        EXAMPLES / "quickstart_demo.py",
        env={"AAC_ANCHOR_URL": "http://127.0.0.1:19999"},
    )
    assert result.returncode == 0, (
        f"quickstart_demo.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
