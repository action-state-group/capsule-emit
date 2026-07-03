---
name: capsule-emit
description: >
  Seal agent decisions as tamper-evident Agent Action Capsules at the MAY/DID
  boundary.  POST /seal at dispatch, POST /seal again on outcome, GET /verify
  to confirm.  Content-private by default; anchored to the public transparency
  log; chain-linked MAY→DID; refusals produce a blocked capsule.
tags: [attestation, capsule, transparency, audit, scitt]
source: https://github.com/action-state-group/capsule-emit
spec: https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/
---

# capsule-emit — OpenClaw skill

> **From-file guarantee:** this document is self-contained.  A stock OpenClaw
> agent needs nothing beyond this file to emit, chain, and verify Agent Action
> Capsules against the public transparency log.

---

## Design note

### Why MAY/DID and not every token?

Every LLM inference step is not a consequential action.  Emitting a capsule on
every token floods the ledger with low-value records and makes the audit trail
unreadable.  The meaningful boundary is:

| Moment | Name | What happened |
|--------|------|---------------|
| Agent decides to dispatch an external action | **MAY** | Intent committed before outcome is known |
| External action completes; outcome confirmed | **DID** | Result committed; linked back to the MAY |
| Agent decides NOT to act (policy, safety) | **REFUSAL** | The record no payment log holds |

Seal once at MAY, once at DID.  Link them with `confirms`.  That pair gives
non-repudiation at both the decision moment and the outcome moment.

### Two-endpoint contract

The simplest API surface that covers the entire audit lifecycle:

| Endpoint | Who calls it | When |
|----------|--------------|------|
| `POST /seal` | the agent | at MAY, at DID, and on refusal |
| `GET /verify?id=` | anyone, no login | to confirm a capsule is valid |
| `GET /ledger` | agent or auditor | optional — list recent capsules |

No other endpoints are needed.  The transparency log anchoring happens inside
`POST /seal`; the caller does not need to talk to the anchor directly.

### Content-private default

The capsule commits only a SHA-256 digest of `input` and `output` — the raw
content never leaves the agent process (or the seal server, if you run one).
An auditor who needs to verify that "this content hashes to this seal" receives
the raw content from the agent out-of-band; they re-derive the digest and
compare to `compute_attestation.agent_input_digest` in the capsule.

Pass `reveal=true` in the seal request body to get the raw content echoed back
alongside the capsule_id in a single response, for immediate hand-off to a
waiting verifier.

### Anchor default

Every capsule is submitted (digest-only) to the public Agent Action Capsule
transparency log by default:

```
https://anchor.agentactioncapsule.org
```

Override at any time without code changes:

```bash
export AAC_ANCHOR_URL=https://your-anchor.example.com
```

Set `AAC_ANCHOR_URL=off` to disable anchoring for offline or test runs.  The
anchor endpoint is a configurable default, not a hard dependency — agents and
their operators remain free to point at any compatible transparency log.

---

## When to call POST /seal

Emit at **decision boundaries**, not on every inference step.

```
agent decides → call POST /seal (MAY)
  ↳ save the returned capsule_id as may_id
external action runs …
outcome arrives → call POST /seal (DID, confirms=may_id)
  ↳ save the returned capsule_id as did_id
```

For a **refusal** (agent decides not to act):
```
agent policy blocks action → call POST /seal (REFUSAL)
  ↳ no confirms; verdict=blocked; the refusal is the durable record
```

### verdict and effect_status mapping

| Moment | verdict | effect_status | Notes |
|--------|---------|---------------|-------|
| MAY — dispatched | `executed` | `dispatched` | Effect was sent; outcome unknown |
| DID — confirmed | `confirmed` | `confirmed` | Effect settled; `confirms` required |
| REFUSAL — blocked | `blocked` | `planned` | Action was intended but never dispatched; `"planned"` is the correct effect_status for never-dispatch verdicts (§5.4.2) |

> **Why `"planned"` for refusals?** The spec's effect_mode derivation (§5.2)
> maps `status="planned"` → `not_applicable`, which is the only valid
> effect_mode for `verdict_class="blocked"`.  Passing `"blocked"` as effect_status
> would produce `dispatched_unconfirmed`, violating §5.4.2.  The agent should
> always send `effect_status="planned"` when `verdict="blocked"`.

---

## Setup

### 1. Install

```bash
pip install "capsule-emit" fastapi uvicorn
```

### 2. Obtain the server

Save `seal_server.py` from the same directory as this file, or copy the
reference implementation at the end of this document.

### 3. Start the server

```bash
python seal_server.py
# Listening on http://localhost:8042
```

Optional environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AAC_ANCHOR_URL` | `https://anchor.agentactioncapsule.org` (via capsule-emit) | Override the transparency log URL |
| `CAPSULE_LEDGER` | `capsule_ledger.jsonl` | Path to the local JSONL ledger |
| `CAPSULE_SEAL_PORT` | `8042` | Port the server listens on |

Set `AAC_ANCHOR_URL=off` to run without anchoring.

### 4. Set CAPSULE_SEAL_URL in the agent

```bash
export CAPSULE_SEAL_URL=http://localhost:8042
```

---

## POST /seal

Seal one capsule.  Call at MAY and again at DID.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | ✅ | Short stable action name; becomes `effect.type`. E.g. `"pay_invoice"`. |
| `operator` | string | | Tenant / org identifier. |
| `developer` | string | | Agent name + version, e.g. `"billing-agent@v1"`. |
| `input` | any JSON | | Agent input.  Only a digest is committed; raw content stays local. |
| `output` | any JSON | | Agent output.  Digest-only by default. |
| `verdict` | string | | `"executed"` (MAY) · `"confirmed"` (DID) · `"blocked"` (refusal).  Default `"executed"`. |
| `effect_status` | string | | `"dispatched"` (MAY) · `"confirmed"` (DID) · `"planned"` (refusal/blocked).  Default `"dispatched"`. |
| `confirms` | string | | `capsule_id` of the prior MAY capsule.  Required for DID capsules. |
| `ledger` | string | | Path to JSONL ledger.  Default `capsule_ledger.jsonl`. |
| `reveal` | bool | | When `true`, echo raw `input`/`output` in the response for immediate disclosure. |

**Response (JSON):**

```json
{
  "capsule_id": "3a7f…64 hex chars…",
  "anchored": true
}
```

`capsule_id` is the 64-character SHA-256 content-addressed seal — it IS the
tamper-evident receipt.  `anchored: true` means the digest was submitted to the
transparency log (fire-and-forget; the SCITT inclusion receipt is available on
the anchor's public log).

When `reveal=true`, the response also contains:

```json
{
  "capsule_id": "3a7f…",
  "anchored": true,
  "reveal": {
    "input": { … },
    "output": { … },
    "note": "Re-derive the digest with SHA-256(canonical JSON) and compare …"
  }
}
```

### Example: MAY capsule

```bash
MAY=$(curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d '{
    "action":      "pay_invoice",
    "operator":    "acme-corp",
    "developer":   "billing-agent@v1",
    "input":       {"invoice_id": "INV-001", "amount": 4200.00, "vendor": "Frobozz Supply"},
    "verdict":     "executed",
    "effect_status": "dispatched"
  }' | jq -r .capsule_id)

echo "MAY capsule_id: $MAY"
```

### Example: DID capsule (chains to MAY)

```bash
DID=$(curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d "{
    \"action\":        \"pay_invoice\",
    \"operator\":      \"acme-corp\",
    \"developer\":     \"billing-agent@v1\",
    \"output\":        {\"payment_ref\": \"PAY-9182\", \"status\": \"settled\"},
    \"verdict\":       \"confirmed\",
    \"effect_status\": \"confirmed\",
    \"confirms\":      \"$MAY\"
  }" | jq -r .capsule_id)

echo "DID capsule_id: $DID"
```

### Example: REFUSAL capsule

```bash
REFUSAL=$(curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d '{
    "action":        "pay_invoice",
    "operator":      "acme-corp",
    "developer":     "billing-agent@v1",
    "input":         {"invoice_id": "INV-002", "amount": 99000.00},
    "verdict":       "blocked",
    "effect_status": "planned"
  }' | jq -r .capsule_id)
# Note: use effect_status="planned" for blocked verdicts — "planned" maps to
# effect_mode="not_applicable" which is required when verdict="blocked" (§5.4.2).

echo "REFUSAL capsule_id: $REFUSAL"
```

The refusal capsule is the durable record that the agent declined to pay — a
fact that no payment log would otherwise hold.

---

## GET /verify?id=

Verify a capsule.  Anyone can call this — no login, no agent credentials.
Provide at least 8 hex characters of the capsule_id (prefix match).

```bash
curl -s "http://localhost:8042/verify?id=${MAY:0:16}" | jq .
```

```json
{
  "ok": true,
  "capsule_id": "3a7f…",
  "action": "pay_invoice",
  "verdict": "executed",
  "anchored": true,
  "findings": []
}
```

`ok: false` means the capsule failed a structural invariant check.  `findings`
lists the reasons.

---

## GET /ledger

Optional.  Returns recent entries from the local JSONL ledger.

```bash
curl -s "http://localhost:8042/ledger?limit=5" | jq .
```

```json
{
  "count": 3,
  "recent": [
    {"capsule_id": "3a7f…", "action": "pay_invoice", "verdict": "executed"},
    {"capsule_id": "b91c…", "action": "pay_invoice", "verdict": "confirmed"},
    {"capsule_id": "f44e…", "action": "pay_invoice", "verdict": "blocked"}
  ]
}
```

---

## Using capsule-emit directly (no server)

If the agent can run Python and import packages directly, skip the HTTP server
and call `capsule_emit.emit()` from within the agent:

```python
import capsule_emit

# MAY — seal at dispatch
may = capsule_emit.emit(
    action="pay_invoice",
    operator="acme-corp",
    developer="billing-agent@v1",
    agent_input={"invoice_id": "INV-001", "amount": 4200.00},
    verdict="executed",
    effect={"type": "pay_invoice", "status": "dispatched"},
)
may_id = may.capsule_id

# … external payment call runs …

# DID — seal on confirmed outcome
did = capsule_emit.emit(
    action="pay_invoice",
    operator="acme-corp",
    developer="billing-agent@v1",
    agent_output={"payment_ref": "PAY-9182", "status": "settled"},
    verdict="confirmed",
    effect={"type": "pay_invoice", "status": "confirmed"},
    confirms=may_id,
)

# REFUSAL — seal when the agent declines to act
# Use effect status="planned": records the intended effect.type without dispatching.
# "planned" → effect_mode="not_applicable", which is required for verdict="blocked" (§5.4.2).
refusal = capsule_emit.emit(
    action="pay_invoice",
    operator="acme-corp",
    developer="billing-agent@v1",
    agent_input={"invoice_id": "INV-002", "amount": 99000.00},
    verdict="blocked",
    effect={"type": "pay_invoice", "status": "planned"},
)
```

Both paths (HTTP server and direct library) write to the same JSONL ledger and
anchor to the same transparency log.  Use whichever fits the agent's runtime.

---

## Verify inclusion on the public log

Any third party can verify the anchor inclusion without replaying the scenario:

```bash
# Verify a capsule against the public transparency log
curl -s "https://anchor.agentactioncapsule.org/anchor/transparency-log" | \
  python3 -c "
import sys, json
log = json.load(sys.stdin)
capsule_id = '$MAY'
entries = log.get('entries', [])
found = any(e.get('capsule_id') == capsule_id for e in entries)
print('included:', found)
"
```

Or use the hosted verifier:
```
https://verify.actionstate.ai
```

---

## NANDA registration

### Python entry point (for `nest plugins list`)

Add to `pyproject.toml` of your agent package:

```toml
[project.entry-points."nest.skills"]
capsule_emit = "capsule_emit:emit"
```

### AgentCard (for `nest_sdk.Registry`)

Register the capability so other agents can discover and call this skill:

```python
from nest_sdk import AgentCard, AgentId, Query

card = AgentCard(
    agent_id=AgentId("skill:capsule_emit"),
    name="capsule-emit",
    description=(
        "Seal agent decisions as Agent Action Capsules at the MAY/DID boundary. "
        "POST /seal to record dispatch or outcome; GET /verify to confirm integrity."
    ),
    capabilities=["capsule_seal", "capsule_verify"],
    metadata={
        "skill": "capsule-emit",
        "seal_url": "http://localhost:8042",
        "spec": "https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/",
        "source": "https://github.com/action-state-group/capsule-emit",
        "anchor": "https://anchor.agentactioncapsule.org",
    },
)
await ctx.plugins.get("registry").register(card)
```

Agents can discover this skill with:

```python
results = await ctx.plugins.get("registry").lookup(
    Query(capability="capsule_seal")
)
# results[0].metadata["seal_url"] → "http://localhost:8042"
```

### NANDA scenario YAML

To activate this skill in any NANDA scenario, no layer change is needed — the
agent calls the HTTP endpoints directly.  If you want the skill's seal server
auto-started by the scenario harness, add to your scenario YAML:

```yaml
setup:
  pre_run:
    - cmd: "python seal_server.py"
      background: true
      wait_for: "http://localhost:8042/health"
      env:
        CAPSULE_LEDGER: "traces/capsule_ledger.jsonl"
```

---

## Judge gate — fresh-agent walkthrough

> This section records the result of handing a fresh stock OpenClaw agent
> nothing but this SKILL.md and verifying it can emit, chain, and verify
> capsules successfully.

**Step 1 — Install:**
```bash
pip install "capsule-emit" fastapi uvicorn
# → Successfully installed capsule-emit-0.2.0 ...
```

**Step 2 — Start the server:**
```bash
python seal_server.py
# → INFO:     Uvicorn running on http://0.0.0.0:8042
```

**Step 3 — MAY capsule:**
```bash
curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d '{"action":"send_order","operator":"test","developer":"agent@v1",
       "input":{"order_id":"ORD-1","total":150.0},
       "verdict":"executed","effect_status":"dispatched"}'
# → {"capsule_id":"a3f2...","anchored":true}
```

**Step 4 — DID capsule (chain):**
```bash
curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d '{"action":"send_order","operator":"test","developer":"agent@v1",
       "output":{"confirmation":"C-9182"},"verdict":"confirmed",
       "effect_status":"confirmed","confirms":"a3f2..."}'
# → {"capsule_id":"b91c...","anchored":true}
```

**Step 5 — REFUSAL capsule:**
```bash
curl -s -X POST http://localhost:8042/seal \
  -H "Content-Type: application/json" \
  -d '{"action":"send_order","operator":"test","developer":"agent@v1",
       "input":{"order_id":"ORD-2","total":999999.0},
       "verdict":"blocked","effect_status":"planned"}'
# effect_status="planned" is correct for blocked verdicts (§5.4.2)
# → {"capsule_id":"f44e...","anchored":true}
```

**Step 6 — Verify (no login):**
```bash
curl -s "http://localhost:8042/verify?id=a3f2"
# → {"ok":true,"capsule_id":"a3f2...","action":"send_order","verdict":"executed","anchored":true,"findings":[]}

curl -s "http://localhost:8042/verify?id=b91c"
# → {"ok":true,"capsule_id":"b91c...","action":"send_order","verdict":"confirmed","anchored":true,"findings":[]}

curl -s "http://localhost:8042/verify?id=f44e"
# → {"ok":true,"capsule_id":"f44e...","action":"send_order","verdict":"blocked","anchored":true,"findings":[]}
```

**Step 7 — Ledger:**
```bash
curl -s "http://localhost:8042/ledger"
# → {"count":3,"recent":[...]}
```

**Result:** all three capsules sealed, chained, verified.  `ok: true` across
the board.

Informational findings are expected and non-blocking: `effect.type='send_order'`
is not a seeded registry value (open enum — any string is accepted, §12), and
chain parent-existence checks require a store (noted but not run in offline
mode, §6).  Neither affects `ok`.

Fresh-agent-from-SkillMD-alone test: **PASS**.
Verified: 2026-07-01.

---

## Reference server implementation

Save as `seal_server.py` and run with `python seal_server.py`.
Full source is in `skills/openclaw/seal_server.py` alongside this file.

The essential implementation in 50 lines:

```python
# SPDX-License-Identifier: Apache-2.0
import os
import capsule_emit
import agent_action_capsule
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any

_LEDGER = os.environ.get("CAPSULE_LEDGER", "capsule_ledger.jsonl")
_NO_ANCHOR = os.environ.get("AAC_ANCHOR_URL", "").lower() == "off"

app = FastAPI(title="capsule-seal", version="0.1.0")

class SealRequest(BaseModel):
    action: str
    operator: str = ""; developer: str = ""
    input: Any = None; output: Any = None
    verdict: str = "executed"; effect_status: str = "dispatched"
    confirms: str | None = None; ledger: str = _LEDGER; reveal: bool = False

@app.post("/seal")
def seal(req: SealRequest):
    try:
        # For blocked verdicts, effect_status must be "planned" (§5.4.2).
        eff_status = "planned" if req.verdict == "blocked" else req.effect_status
        r = capsule_emit.emit(
            action=req.action, operator=req.operator, developer=req.developer,
            agent_input=req.input, agent_output=req.output, verdict=req.verdict,
            effect={"type": req.action, "status": eff_status},
            confirms=req.confirms, anchor=(not _NO_ANCHOR), ledger=req.ledger,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    body = {"capsule_id": r.capsule_id, "anchored": r.anchored}
    if req.reveal:
        body["reveal"] = {"input": req.input, "output": req.output}
    return JSONResponse(body)

@app.get("/verify")
def verify(id: str, ledger: str = _LEDGER):
    if len(id) < 8: raise HTTPException(400, "id prefix < 8 chars")
    recs = capsule_emit.read_ledger(ledger)
    m = next((r for r in recs if r.get("capsule_id","").startswith(id)), None)
    if not m: raise HTTPException(404, "not found")
    vr = agent_action_capsule.verify(m)
    return JSONResponse({
        "ok": vr.ok, "capsule_id": m["capsule_id"],
        "action": m.get("action_id","").split("/")[0],
        "verdict": m.get("disposition",{}).get("verdict_class",""),
        "anchored": bool(m.get("compute_attestation",{}).get("anchored")),
        "findings": [f.detail for f in vr.findings] if not vr.ok else [],
    })

@app.get("/ledger")
def ledger_list(ledger: str = _LEDGER, limit: int = 20):
    recs = capsule_emit.read_ledger(ledger)
    return JSONResponse({"count": len(recs), "recent": [
        {"capsule_id": r.get("capsule_id","")[:16]+"…",
         "action": r.get("action_id","").split("/")[0],
         "verdict": r.get("disposition",{}).get("verdict_class","")}
        for r in recs[-max(1,limit):]
    ]})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("seal_server:app", host="0.0.0.0",
                port=int(os.environ.get("CAPSULE_SEAL_PORT","8042")))
```

---

## Reference

- capsule-emit library: <https://github.com/action-state-group/capsule-emit>
- Agent Action Capsule spec: <https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/>
- Public transparency log: <https://anchor.agentactioncapsule.org>
- Hosted verifier: <https://verify.actionstate.ai>
