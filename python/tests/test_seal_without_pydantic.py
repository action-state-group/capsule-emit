# SPDX-License-Identifier: Apache-2.0
"""seal() works with no pydantic installed or imported (capsule-emit#426)."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_seal_never_imports_pydantic(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        from capsule_emit import seal
        r = seal({"a": [1, "x"]}, action="t", anchor=False, witness=False, ledger=sys.argv[1])
        assert r.capsule
        assert "pydantic" not in sys.modules
        """
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "ledger")], check=True)


def test_seal_works_when_pydantic_is_absent(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        sys.modules["pydantic"] = None  # any import of pydantic now fails
        from capsule_emit import seal
        r = seal({"a": 1}, action="t", anchor=False, witness=False, ledger=sys.argv[1])
        assert r.capsule
        """
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "ledger")], check=True)


def test_seal_does_not_trigger_pydantic_lazy_imports(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        import pydantic
        before = "pydantic.main" in sys.modules
        from capsule_emit import seal
        seal({"a": 1}, action="t", anchor=False, witness=False, ledger=sys.argv[1])
        assert ("pydantic.main" in sys.modules) == before
        """
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "ledger")], check=True)
