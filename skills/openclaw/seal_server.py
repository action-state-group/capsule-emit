# SPDX-License-Identifier: Apache-2.0
"""seal_server.py — thin HTTP facade over capsule-emit for OpenClaw agents.

Two required endpoints:
  POST /seal             seal a MAY or DID capsule; returns capsule_id
  GET  /verify?id=<id>   verify any capsule by id prefix; no login required

One optional endpoint:
  GET  /ledger           list recent capsules in the local ledger

Run:
  pip install "capsule-emit" fastapi uvicorn
  python seal_server.py          # listens on :8042

Override the anchor:
  AAC_ANCHOR_URL=https://your-anchor.example.com python seal_server.py

The server defaults to anchoring every capsule to the public Agent Action
Capsule transparency log (anchor.agentactioncapsule.org) via the AAC_ANCHOR_URL
env var.  Set AAC_ANCHOR_URL=off to disable anchoring for offline use.
"""
from __future__ import annotations

import os
from typing import Any

import agent_action_capsule

import capsule_emit

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError as exc:
    raise SystemExit("pip install fastapi uvicorn") from exc

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER = os.environ.get("CAPSULE_LEDGER", "capsule_ledger.jsonl")
_ANCHOR_OFF = os.environ.get("AAC_ANCHOR_URL", "").lower() == "off"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="capsule-seal",
    description=(
        "Thin seal/verify facade for OpenClaw agents. "
        "POST /seal at the MAY boundary and again at DID. "
        "GET /verify?id= for anyone to verify without login."
    ),
    version="0.1.0",
)


class SealRequest(BaseModel):
    """Body for POST /seal."""

    action: str
    """Short stable action name, e.g. 'pay_invoice'.  Becomes effect.type."""

    operator: str = ""
    """Tenant / org identifier stamped on every capsule."""

    developer: str = ""
    """Agent name + version, e.g. 'billing-agent@v1'."""

    input: Any = None
    """Agent input (any JSON-serialisable value). Only a SHA-256 digest is
    committed to the capsule — raw content never leaves this process unless
    you pass reveal=true."""

    output: Any = None
    """Agent output.  Digest-only by default (same as input)."""

    verdict: str = "executed"
    """'executed' (MAY), 'confirmed' (DID), or 'blocked' (refusal)."""

    effect_status: str = "dispatched"
    """'dispatched' (MAY), 'confirmed' (DID), or 'blocked' (refusal)."""

    confirms: str | None = None
    """capsule_id of the prior MAY capsule.  Required for DID capsules."""

    ledger: str = _DEFAULT_LEDGER
    """Path to the JSONL ledger file (default: capsule_ledger.jsonl)."""

    reveal: bool = False
    """When True, echo raw input/output back in the response alongside the
    capsule_id so the caller can share them with verifiers who need to
    confirm the content hashes to the sealed digest."""


@app.post("/seal")
def seal(req: SealRequest) -> JSONResponse:
    """Seal one capsule.  Call at the MAY boundary and again at DID.

    Returns:
        capsule_id   — 64-char hex SHA-256 seal (the tamper-evident receipt)
        anchored     — True when the digest was submitted to the transparency log
        reveal       — present only when reveal=True; contains raw input/output
    """
    try:
        # For blocked verdicts, effect_status must be "planned" so the effect_mode
        # derives to "not_applicable" — the only valid mode for never-dispatch
        # verdict classes (§5.4.2).  Apply this remap as a safety net regardless
        # of what the caller sends.
        eff_status = "planned" if req.verdict == "blocked" else req.effect_status
        result = capsule_emit.emit(
            action=req.action,
            operator=req.operator,
            developer=req.developer,
            agent_input=req.input,
            agent_output=req.output,
            verdict=req.verdict,
            effect={"type": req.action, "status": eff_status},
            confirms=req.confirms,
            anchor=(not _ANCHOR_OFF),
            ledger=req.ledger,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body: dict[str, Any] = {
        "capsule_id": result.capsule_id,
        "anchored": result.anchored,
    }
    if req.reveal:
        body["reveal"] = {
            "input": req.input,
            "output": req.output,
            "note": (
                "Re-derive the digest with SHA-256(json.dumps(value, sort_keys=True, "
                "separators=(',',':'))) and compare to compute_attestation."
                "agent_input_digest / agent_output_digest in the capsule."
            ),
        }
    return JSONResponse(body)


@app.get("/verify")
def verify(id: str, ledger: str = _DEFAULT_LEDGER) -> JSONResponse:
    """Verify a capsule by id prefix (minimum 8 hex chars).  No login required.

    Anyone — including third parties who ran none of the agents — can call this
    endpoint to confirm a capsule is structurally valid.

    Returns:
        ok         — True when the capsule passes all invariant checks
        capsule_id — full 64-char id
        action     — the action name sealed in this capsule
        verdict    — 'executed' | 'confirmed' | 'blocked'
        anchored   — True when the capsule was submitted to the anchor
        findings   — list of detail strings when ok=False
    """
    if len(id) < 8:
        raise HTTPException(
            status_code=400,
            detail=f"id prefix too short ({len(id)} chars); provide at least 8",
        )
    records = capsule_emit.read_ledger(ledger)
    match = next(
        (r for r in records if r.get("capsule_id", "").startswith(id)),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail=f"not found: {id!r}")

    try:
        vr = agent_action_capsule.verify(match)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"verify error: {exc}") from exc

    return JSONResponse({
        "ok": vr.ok,
        "capsule_id": match["capsule_id"],
        "action": match.get("action_id", "").split("/")[0],
        "verdict": match.get("disposition", {}).get("verdict_class", ""),
        "anchored": bool(match.get("compute_attestation", {}).get("anchored")),
        "findings": [f.detail for f in vr.findings] if not vr.ok else [],
    })


@app.get("/ledger")
def ledger_view(ledger: str = _DEFAULT_LEDGER, limit: int = 20) -> JSONResponse:
    """Return recent capsules from the local ledger.  Optional convenience endpoint."""
    records = capsule_emit.read_ledger(ledger)
    limit = max(1, limit)
    recent = records[-limit:]
    return JSONResponse({
        "count": len(records),
        "recent": [
            {
                "capsule_id": r.get("capsule_id", "")[:16] + "…",
                "action": r.get("action_id", "").split("/")[0],
                "verdict": r.get("disposition", {}).get("verdict_class", ""),
            }
            for r in recent
        ],
    })


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": capsule_emit.__version__}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("CAPSULE_SEAL_PORT", "8042"))
    uvicorn.run("seal_server:app", host="0.0.0.0", port=port, reload=False)
