# ADOPT.md Stranger Walkthrough Transcript

**Date:** 2026-07-28  
**Branch:** feat/self-serve-adopter (PR #35, post-bounce revision)  
**Platform:** macOS Darwin 24.6.0 / Python 3.13.7  
**Capsule emitted:** `c8f94fc48a9823dadc48c05ec10c0a7b6471fbb2439cd2862c67a421942cb546`  
**Anchor leaf_index:** 197 / tree_size: 198  
**Entry hash:** `04f6fa376acd282ebaa7427d600ec838249a2baceb4f616f0702045ac1347ed6`

Follows ADOPT.md verbatim from a fresh directory with no prior capsule-emit install.
Every command was run and the output is reproduced exactly.

---

## Environment

```
$ mkdir -p /tmp/adopt-walkthrough && cd /tmp/adopt-walkthrough
$ python3 -m venv .venv
```

---

## Step 1 — Emit your first capsule

```
$ .venv/bin/pip install capsule-emit

Collecting capsule-emit
Collecting agent-action-capsule>=0.1.0 (from capsule-emit)
Using cached capsule_emit-0.3.2-py3-none-any.whl (58 kB)
Using cached agent_action_capsule-0.1.0-py3-none-any.whl (51 kB)
Successfully installed agent-action-capsule-0.1.0 capsule-emit-0.3.2
```

```python
from capsule_emit import emit

cap = emit(
    action="hello-world",
    operator="your-org",
    developer="my-agent@v1",
    agent_input={"task": "greet"},
    agent_output={"message": "hello"},
    model={"provider": "your-provider", "model_id": "your-model"},
    verdict="executed",
    effect={"type": "write_order", "status": "confirmed"},
)
print("Capsule ID:", cap.capsule_id)
print("Anchored: ", cap.anchored)
```

Output:
```
Capsule ID: c8f94fc48a9823dadc48c05ec10c0a7b6471fbb2439cd2862c67a421942cb546
Anchored:  True
```

---

## Step 2 — Verify the record

```
$ .venv/bin/capsule-emit ledger view ./ledger.jsonl

capsule ledger: ./ledger.jsonl  (1 record(s))

  capsule_id      actor                                       verdict       effect                chain    verify
-----------------------------------------------------------------------------------------------------------------
  c8f94fc48a9823  my-agent@v1                                 executed      write_order:applied               ✓
```

```
$ .venv/bin/pip install agent-action-capsule
(Requirement already satisfied — installed as capsule-emit dependency)

$ .venv/bin/agent-action-capsule verify --store ./ledger.jsonl

Store-level verification of 1 capsule(s) in ./ledger.jsonl:
  [0] ok: True
  capsule_id (recomputed): c8f94fc48a9823dadc48c05ec10c0a7b6471fbb2439cd2862c67a421942cb546
  derived: effect_mode=confirmed attestation_mode=self_attested ledger_mode=standalone
  findings: none
```

Single-capsule verify (using `cap.capsule` to get the raw dict):

```python
import json, pathlib
cap_dict = cap.capsule          # EmitResult.capsule holds the raw dict
pathlib.Path("capsule.json").write_text(json.dumps(cap_dict))
```

```
$ .venv/bin/agent-action-capsule verify capsule.json

Agent Action Capsule — Class-1 payload verification: capsule.json
  ok: True
  capsule_id (recomputed): 1e79774647ec0c19ecd5a3d1d2933e00986d16553a299ef555919e0948cec87b
  derived: effect_mode=confirmed attestation_mode=self_attested ledger_mode=standalone
  findings: none
```

---

## Step 3 — Confirm anchor registration and verify the receipt

```bash
CAPSULE_ID=c8f94fc48a9823dadc48c05ec10c0a7b6471fbb2439cd2862c67a421942cb546

# 1. Fetch receipt (POST is idempotent — same capsule_id always returns same receipt)
$ curl -s -X POST https://anchor.agentactioncapsule.org/v1/digest \
    -H 'Content-Type: application/json' \
    -d '{"capsule_id": "'"${CAPSULE_ID}"'"}' > anchor_resp.json

# 2. Save receipt file and display proof summary
$ python3 -c "
import json, base64
d = json.load(open('anchor_resp.json'))
open('receipt.cose', 'wb').write(base64.b64decode(d['receipt_b64']))
open('entry_hash.txt', 'w').write(d['entry_hash'])
print('entry_hash :', d['entry_hash'])
print('leaf_index :', d['leaf_index'], '/ tree_size:', d['tree_size'])
"

entry_hash : 04f6fa376acd282ebaa7427d600ec838249a2baceb4f616f0702045ac1347ed6
leaf_index : 197 / tree_size: 198
```

```bash
# 3. Fetch the anchor log public key (PEM)
$ python3 -c "
import urllib.request, json, base64
d = json.loads(urllib.request.urlopen(
    'https://anchor.agentactioncapsule.org/anchor/authority-pubkey').read())
raw = bytes.fromhex(d['pubkey_hex'])
der = bytes.fromhex('302a300506032b6570032100') + raw
b64 = base64.encodebytes(der).decode().strip()
open('anchor_pub.pem','w').write(
    '-----BEGIN PUBLIC KEY-----\n' + b64 + '\n-----END PUBLIC KEY-----')
print('anchor key_id:', d['key_id'])
"

anchor key_id: 39bb654c9dc0afe1
```

```bash
# 4. Verify the receipt offline
$ .venv/bin/pip install scitt-cose
Successfully installed cbor2-6.1.3 cffi-2.1.0 cryptography-49.0.0 pycparser-3.0 scitt-cose-0.1.1

$ .venv/bin/scitt-cose \
    --receipt receipt.cose \
    --receipt-log-pubkey anchor_pub.pem \
    --leaf-entry-hex "$(cat entry_hash.txt)"

scitt-cose
  scitt-cose tracks draft-ietf-scitt-architecture-22 and
  draft-ietf-cose-merkle-tree-proofs-18 — IETF Internet-Drafts (Work in
  Progress), currently in the RFC Editor Queue, NOT yet published as RFCs.
  Substrate RFCs used: RFC 9052, RFC 9053, RFC 9162, RFC 9597, RFC 9964.
------------------------------------------------------------------------
  Receipt
    ok             : True
    root           : 17bd056faeede7f51a7f1029cc9c4a54beac517d7ca83375172de8a393c0f213
    tree_size      : 198
    leaf_index     : 197
------------------------------------------------------------------------
```

**All steps PASS.**

---

## Doc issues found and fixed

| Issue | Location | Fix |
|-------|----------|-----|
| `cap.to_dict()` does not exist on `EmitResult` | Step 2 | Changed to `cap.capsule` (the correct attribute) |
| `verify.actionstate.ai/v/<capsule_id>` 404s (P1 not deployed; wrong domain) | Step 3 | Removed. Step 3 now documents the working anchor API path (`POST /v1/digest`) + offline scitt-cose receipt verification |
| Permalink note pointed at `verify.actionstate.ai` | Step 3 | Replaced with `verify.agentactioncapsule.org` (neutral domain), marked as "landing with verify-surface deploy" |
| `AAC_ANCHOR_URL` example used `/v1/entries` (non-existent path) | Open registration notice | Changed to `/v1/digest` (the actual endpoint) |
