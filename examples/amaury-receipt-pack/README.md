# Agent Action Capsule — Sample Receipt Pack

Prepared for SCITT interop exploration (Vienna 2026).

## Contents

| File | Description |
|------|-------------|
| `sample_ledger.jsonl` | 4 sample capsules (executed, blocked/refusal, gate\_checks, chained) |
| `generate.py` | Script that reproduces all 4 capsules from scratch |
| `receipts/README-receipts.md` | Anchor endpoint, receipt structure, pyscitt verify walkthrough |

## Capsule summary

| # | Action | Verdict | Notes |
|---|--------|---------|-------|
| 1 | `approve_purchase` | `executed` | Standard consequential action; effect dispatched |
| 2 | `transfer_funds` | `blocked` | Policy refusal; constraint `transfer_limit_eur_check` → `fail` |
| 3 | `generate_report` | `executed` | Two gate constraints passing (`value_grounded`, `invoice_reconciles`) |
| 4 | `confirm_purchase` | `confirmed` | Chained to capsule #1 via `chain.relation = "confirms"` |

All capsules are emitted with `anchor=False`; no network calls are made by
the generator. Capsule IDs are SHA-256 content-addresses over the canonical
capsule JSON (§5.1 of the individual I-D).

## Verify locally (agent-action-capsule verifier)

```bash
pip install agent-action-capsule capsule-emit
python3 generate.py          # produces sample_ledger.jsonl
capsule-emit verify --store sample_ledger.jsonl
```

Expected output: `4/4 VALID` (Class-1 verification — structure, digest, and
disposition invariants; substrate receipt verification is Class 2).

## Reproduce from scratch

```bash
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
pip install -e ".[dev]"
cd examples/amaury-receipt-pack
python3 generate.py
capsule-emit verify --store sample_ledger.jsonl
```

## Anchor endpoint (RFC9162\_SHA256 receipts)

Submit any capsule to the live transparency log:

```
POST https://anchor.agentactioncapsule.org/anchor
Content-Type: application/json

{"payload": "<base64url of capsule JSON>"}
```

The response contains an RFC9162\_SHA256 COSE receipt. See
`receipts/README-receipts.md` for the full request/response structure and
pyscitt verification steps.

## Two-TS interop goal

The same capsule should verify under:

1. **Our anchor** (`anchor.agentactioncapsule.org`) — RFC9162\_SHA256 VDS,
   capsule-anchor implementation
2. **A CCF-backed transparency service** — pyscitt / microsoft/CCF

Both are conforming SCITT Transparency Services per RFC 9943
(draft-ietf-scitt-architecture). The capsule payload is VDS-agnostic: the
same JSON bytes anchor to any conforming TS without modification. The
`scitt-cose` verifier (`pip install scitt-cose`) can verify the receipt
independently of the producing stack.

## Chain structure

```
capsule #1 (approve_purchase / executed)
    └─ [confirms] capsule #4 (confirm_purchase / confirmed)
```

Capsules #2 and #3 are standalone roots (no chain parent).

## Specification

Individual I-D: <https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/>
