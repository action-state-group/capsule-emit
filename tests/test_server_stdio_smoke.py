# SPDX-License-Identifier: Apache-2.0
"""Stdio smoke test for the capsule_emit companion MCP server (capsule-emit#184).

Launches `python -m capsule_emit.server` as a real subprocess and drives it
over stdio with the mcp SDK's own ClientSession — proving the server starts
and answers tools/list under whichever mcp major is installed (1.x or 2.x).
Skipped automatically when `mcp` is not installed.

Run with: pip install "capsule-emit[mcp]" && pytest -m mcp tests/test_server_stdio_smoke.py
"""
from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("mcp", reason="mcp not installed")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


async def _list_tools(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "capsule_emit.server"],
        env={"CAPSULE_LEDGER": str(tmp_path / "ledger.jsonl")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5)
            result = await asyncio.wait_for(session.list_tools(), timeout=5)
            return [t.name for t in result.tools]


@pytest.mark.mcp
def test_server_answers_tools_list_over_stdio(tmp_path):
    """python -m capsule_emit.server starts and answers tools/list over stdio.

    One test body, run under whichever mcp major the active venv has
    installed (1.x or 2.x) — the import shim in capsule_emit/server.py is
    what makes both work.
    """
    names = asyncio.run(_list_tools(tmp_path))
    assert set(names) == {"capsule_record", "capsule_verify", "capsule_ledger"}
