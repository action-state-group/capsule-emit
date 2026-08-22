# SPDX-License-Identifier: Apache-2.0
"""Layer-0 import-cost gate: ``capsule_emit.checkpoint`` (the CLL/MMR
subpackage) must never load unless a caller explicitly opts in.

Run in a fresh subprocess so no other test in the suite -- which may itself
import ``capsule_emit.checkpoint`` -- can leave it warm in ``sys.modules``
and mask a regression here.
"""
from __future__ import annotations

import subprocess
import sys


def test_importing_capsule_emit_does_not_load_checkpoint_subpackage():
    script = (
        "import sys, capsule_emit\n"
        "assert 'capsule_emit.checkpoint' not in sys.modules, "
        "'capsule_emit.checkpoint loaded on bare `import capsule_emit` -- Layer-0 cost regression'\n"
        "assert 'capsule_emit.checkpoint.core' not in sys.modules\n"
        "assert 'capsule_emit.checkpoint.emit' not in sys.modules\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_importing_capsule_emit_submodules_does_not_load_checkpoint():
    """Same guarantee for the CLI/server/adapters entry points -- none of
    capsule-emit's own modules may reach for `checkpoint` implicitly."""
    script = (
        "import sys\n"
        "import capsule_emit.cli\n"
        "import capsule_emit.core\n"
        "import capsule_emit.ledger\n"
        "assert 'capsule_emit.checkpoint' not in sys.modules\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_checkpoint_subpackage_imports_cleanly_when_opted_in():
    """The mutant this check guards against a false positive on: confirm the
    opt-in import path actually works (not just "never imported")."""
    script = (
        "from capsule_emit.checkpoint import MmrLedger, CheckpointConfig, emit_checkpoint\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
