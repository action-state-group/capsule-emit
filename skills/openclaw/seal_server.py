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

Every sealed capsule goes into a signed local ledger. Once CAPSULE_WITNESS_URL
names a witness, async, digest-only CLL checkpoints of that ledger are built
and registered with it (see docs/checkpoint.md) -- there is no default
witness. Opt out
entirely (witness=False / CAPSULE_WITNESS=off) for a genuinely offline
deployment.
The older, non-default per-capsule anchor channel this server used to
re-enable unconditionally is legacy and stays off unless you explicitly set
CAPSULE_ANCHOR=legacy-on -- see capsule_emit.core's "Single egress" note.
"""
from __future__ import annotations

import json
import os
import warnings
from typing import Any

import agent_action_capsule

import capsule_emit
from capsule_emit.ledger import read_ledger_entries
from capsule_emit.ledger_layout import LedgerDeprecationWarning

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError as exc:
    raise SystemExit("pip install fastapi uvicorn") from exc

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER = os.environ.get("CAPSULE_LEDGER", "capsule-ledger")

# Before capsule-emit 0.13 this server's default ledger was a flat file. A
# server upgraded in place would start a second history in capsule-ledger/
# next to it, so it says so: at startup, and again at the first record it
# writes there while the old file is still beside it.
_OLD_DEFAULT_LEDGER = "capsule_ledger.jsonl"
_split_warned: set[str] = set()
_settled: bool | None = None  # _history_splits() once the new ledger exists


def _first_id(path: str) -> str | None:
    """The first capsule_id in a ledger, reading a flat file only up to it."""
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("capsule_id"):
                    return entry["capsule_id"]
        return None
    for entry in read_ledger_entries(path):
        if entry.get("capsule_id"):
            return entry["capsule_id"]
    return None


def _history_splits() -> bool:
    """The default ledger is a ledger directory, and the old flat default
    beside it holds records the directory does not start with (it was not
    imported). Once the directory exists the answer cannot change, so it is
    worked out once; until then only the old file's first line is read."""
    global _settled
    if _settled is not None:
        return _settled
    if "CAPSULE_LEDGER" in os.environ or not os.path.isfile(_OLD_DEFAULT_LEDGER):
        return False
    old_first = _first_id(_OLD_DEFAULT_LEDGER)
    if old_first is None:
        return False
    if not os.path.exists(_DEFAULT_LEDGER):
        return True
    _settled = _first_id(_DEFAULT_LEDGER) != old_first
    return _settled


def _warn_if_history_splits(*, at_startup: bool = False) -> None:
    """Warn once per process at startup, and once at the first record, while
    the old flat default holds history the default ledger directory lacks."""
    case = "startup" if at_startup else "record"
    if case in _split_warned or not _history_splits():
        return
    if at_startup and not os.path.exists(_DEFAULT_LEDGER):
        message = (
            f"capsule-emit: {_OLD_DEFAULT_LEDGER} holds this server's earlier records, and new records "
            f"now go to ./{_DEFAULT_LEDGER}/. To keep one history, before the first new record run: "
            f"capsule-emit ledger import {_OLD_DEFAULT_LEDGER} {_DEFAULT_LEDGER} "
            f"(or set CAPSULE_LEDGER={_OLD_DEFAULT_LEDGER} to keep writing it, with a deprecation "
            "warning, until capsule-emit 0.15.0)."
        )
    else:
        message = (
            f"capsule-emit: {_OLD_DEFAULT_LEDGER} holds this server's earlier records and "
            f"./{_DEFAULT_LEDGER}/ its new ones, so its history is in two ledgers. Both stay "
            f"readable. Set CAPSULE_LEDGER={_OLD_DEFAULT_LEDGER} to keep writing the old one."
        )
    _split_warned.add(case)
    warnings.warn(message, LedgerDeprecationWarning, stacklevel=2)


_warn_if_history_splits(at_startup=True)

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
    """Path to the ledger: a ledger store directory, or an existing flat ledger file (default: capsule-ledger)."""

    reveal: bool = False
    """When True, echo raw input/output back in the response alongside the
    capsule_id so the caller can share them with verifiers who need to
    confirm the content hashes to the sealed digest."""


@app.post("/seal")
def seal(req: SealRequest) -> JSONResponse:
    """Seal one capsule.  Call at the MAY boundary and again at DID.

    Returns:
        capsule_id   — 64-char hex SHA-256 seal (the tamper-evident record)
        anchored     — legacy, non-default channel; always False unless a
                       prior deployment explicitly opted in via
                       CAPSULE_ANCHOR=legacy-on (see the module docstring).
                       Witnessing (the CLL checkpoint stream) is the real,
                       default egress path and is not reported per-call —
                       see capsule_emit's `status` verb / docs/checkpoint.md.
        anchor_status — "confirmed" | "submitted" | "failed" | "skipped"
                       ("skipped" is the expected value by default, 0.5.0+)
        reveal       — present only when reveal=True; contains raw input/output
    """
    try:
        # For blocked verdicts, effect_status must be "planned" so the effect_mode
        # derives to "not_applicable" — the only valid mode for never-dispatch
        # verdict classes (§5.4.2).  Apply this remap as a safety net regardless
        # of what the caller sends.
        eff_status = "planned" if req.verdict == "blocked" else req.effect_status
        # anchor= is deliberately NOT passed here -- it defaults to off
        # (capsule_emit.core's "single egress" default; see the module
        # docstring). This server used to force anchor=True by default
        # (`anchor=(not _ANCHOR_OFF)`), re-enabling the exact per-record
        # egress the single-egress change removed -- the regression this fix removes.
        if req.ledger == _DEFAULT_LEDGER:
            _warn_if_history_splits()
        result = capsule_emit.seal(
            req.input,
            action=req.action,
            operator=req.operator,
            developer=req.developer,
            agent_output=req.output,
            verdict=req.verdict,
            effect={"type": req.action, "status": eff_status},
            confirms=req.confirms,
            ledger=req.ledger,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body: dict[str, Any] = {
        "capsule_id": result.capsule_id,
        "anchored": result.anchored,
        "anchor_status": result.anchor_status,
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
        findings   — list of detail strings when ok=False

    Anchor status is a property of /seal's EmitResult, not the ledger record,
    so it is intentionally not reported here (a prior version returned an
    "anchored" field read from a ledger key capsule_emit never writes — always
    False, a claim this endpoint had no basis for).
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
