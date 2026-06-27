# SPDX-License-Identifier: Apache-2.0
"""capsule-emit companion MCP server — Goose extension (record / verify / ledger).

Run as a Goose extension so Goose (or any MCP client) can record, verify, and
inspect Agent Action Capsules from any session.  Pair it with Pattern A (below)
to seal calls inside your own MCP server tools, then query this server to
inspect the ledger and verify capsules.

    python -m capsule_emit.server       # stdio — Goose extension default

Pattern A (in YOUR MCP server): seal at the tool
-------------------------------------------------
The recommended pattern — every tool call is sealed automatically:

    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="goose-agent@v1",
        anchor=False,  # True for live anchoring
    )

    @server.tool()          # MCP framework layer
    @emitter.tool()         # capsule-emit record layer (inner)
    def submit_order(vendor: str, amount: float) -> dict:
        ...

Pattern B (this server): the companion server
---------------------------------------------
Add this server to Goose to give the agent tools to record arbitrary tool
calls, verify capsules, and inspect the ledger.  Add to
~/.config/goose/config.yaml:

    extensions:
      capsule_emit:
        enabled: true
        type: stdio
        name: capsule_emit
        description: "Record + verify Agent Action Capsules"
        cmd: python3
        args: ["-m", "capsule_emit.server"]
        timeout: 30
        envs:
          CAPSULE_LEDGER: "/tmp/goose-capsules.jsonl"
          CAPSULE_OPERATOR: "my-org"
          CAPSULE_DEVELOPER: "goose-agent@v1"

Or with uvx (requires capsule-emit[mcp] installed):

    cmd: uvx
    args: ["--from", "capsule-emit[mcp]", "capsule-emit-server"]

Environment variables
---------------------
    CAPSULE_LEDGER    Path to JSONL ledger (default: ledger.jsonl)
    CAPSULE_OPERATOR  Tenant / org identifier stamped on every capsule
    CAPSULE_DEVELOPER Agent name + version

Requires: pip install "capsule-emit[mcp]"
"""
from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

from capsule_emit import emit, read_ledger

try:
    from agent_action_capsule import verify as _aac_verify

    _VERIFY_OK = True
except ImportError:
    _VERIFY_OK = False

_LEDGER = os.environ.get("CAPSULE_LEDGER", "ledger.jsonl")
_OPERATOR = os.environ.get("CAPSULE_OPERATOR", "goose-user")
_DEVELOPER = os.environ.get("CAPSULE_DEVELOPER", "goose-agent@v1")

mcp = FastMCP(
    "capsule-emit",
    instructions=(
        "Record consequential tool calls as verifiable Agent Action Capsules. "
        "Use capsule_record to seal a tool call, capsule_verify to check a "
        "capsule by ID, and capsule_ledger to inspect the local audit trail."
    ),
)


@mcp.tool()
def capsule_record(
    action: str,
    tool_input: str,
    tool_output: str,
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str = _LEDGER,
) -> str:
    """Seal a tool call as a verifiable Agent Action Capsule.

    Args:
        action: The tool or action name (e.g. "submit_order").
        tool_input: JSON-encoded input dict (will be digest-committed).
        tool_output: JSON-encoded output dict (will be digest-committed).
        operator: Tenant / org identifier (default: CAPSULE_OPERATOR env).
        developer: Agent name + version (default: CAPSULE_DEVELOPER env).
        ledger: Path to the JSONL ledger file.
    """
    try:
        inp = json.loads(tool_input) if isinstance(tool_input, str) else tool_input
    except Exception:
        inp = tool_input
    try:
        out = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
    except Exception:
        out = tool_output

    result = emit(
        action=action,
        operator=operator,
        developer=developer,
        agent_input=inp,
        agent_output=out,
        verdict="executed",
        effect={"type": action, "status": "dispatched"},
        anchor=False,
        ledger=ledger,
        runtime="mcp",
    )
    return f"sealed capsule_id={result.capsule_id}"


@mcp.tool()
def capsule_verify(capsule_id: str, ledger: str = _LEDGER) -> str:
    """Verify an Agent Action Capsule by ID (full or prefix, minimum 8 hex chars).

    Looks up the capsule in the ledger and returns ok=True/False plus any
    structural or digest findings.

    Args:
        capsule_id: Full or prefix capsule ID (minimum 8 hex chars required
            to avoid ambiguous prefix matches across large ledgers).
        ledger: Path to the JSONL ledger file.
    """
    if not _VERIFY_OK:
        return "error: agent-action-capsule not installed; pip install agent-action-capsule"
    if len(capsule_id) < 8:
        return (
            f"error: capsule_id prefix too short ({len(capsule_id)} chars); "
            "provide at least 8 hex characters to avoid ambiguous matches"
        )
    records = read_ledger(ledger)
    match = next(
        (r for r in records if r.get("capsule_id", "").startswith(capsule_id)),
        None,
    )
    if match is None:
        return f"not_found capsule_id={capsule_id!r} ledger={ledger}"
    vr = _aac_verify(match)
    if vr.ok:
        return f"ok=True capsule_id={match['capsule_id']}"
    findings = "; ".join(f.detail for f in vr.findings)
    return f"ok=False capsule_id={match['capsule_id']} findings: {findings}"


@mcp.tool()
def capsule_ledger(ledger: str = _LEDGER, limit: int = 20) -> str:
    """Summarise the Agent Action Capsule ledger (most recent rows first).

    Args:
        ledger: Path to the JSONL ledger file.
        limit: Maximum number of rows to return (default 20, minimum 1).
    """
    records = read_ledger(ledger)
    if not records:
        return f"empty ledger: {ledger}"
    # Clamp limit: Python's -N[:] silently drops the first rows; 0 returns everything.
    limit = max(1, limit)
    rows = records[-limit:]
    lines = [f"ledger: {ledger} — {len(records)} capsule(s), showing last {len(rows)}"]
    for r in rows:
        cid = r.get("capsule_id", "?")[:12]
        action = r.get("action_id", "?").split("/")[0]
        verdict = r.get("disposition", {}).get("verdict_class", "?")
        lines.append(f"  {cid}… {action} [{verdict}]")
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
