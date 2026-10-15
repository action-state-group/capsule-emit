# SPDX-License-Identifier: Apache-2.0
"""The Python interlock adapter reproduces the interlock's golden reports."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

VECTORS = Path(__file__).resolve().parents[2] / "test-vectors" / "artifact-store"
spec = importlib.util.spec_from_file_location(
    "artifact_interlock", Path(__file__).resolve().parents[1] / "scripts" / "artifact_interlock.py"
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.mark.parametrize("fmt", ["jsonl", "sqlite"])
def test_reproduces_golden_reports(fmt, tmp_path):
    target = str(tmp_path / f"store.{fmt}")
    for phase in ("write", "read", "strict", "check"):
        out = io.StringIO()
        adapter.run([phase, fmt, target, "interlock", str(VECTORS)], out)
        assert out.getvalue() == (VECTORS / f"interlock-{phase}.txt").read_text(encoding="utf-8"), (fmt, phase)


def test_rejects_bad_arguments(tmp_path):
    with pytest.raises(SystemExit):
        adapter.run([], io.StringIO())
    with pytest.raises(SystemExit):
        adapter.run(["write", "csv", "x", "ns", str(VECTORS)], io.StringIO())
    with pytest.raises(SystemExit):
        adapter.run(["scan", "jsonl", str(tmp_path / "a"), "ns", str(VECTORS)], io.StringIO())
    assert adapter.outcome(lambda: (_ for _ in ()).throw(OSError("disk")))[0] == "storage_error"
