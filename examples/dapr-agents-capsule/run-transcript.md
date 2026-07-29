# Dapr Agents Demo — Run Transcript

Run: `python3 examples/dapr-agents-capsule/demo.py`  
Anchor: `https://anchor.agentactioncapsule.org` (production)  
Branch: `feat/dapr-agents-adapter` (post-rebase)

---

## Live capsule IDs

| Capsule | capsule_id | leaf_index | tree_size |
|---------|-----------|-----------|-----------|
| fyi (check_invoice) | `56a0c398335c624cb7271108d0d9c8cad6b8912238c625985d9e3c36af7e61d5` | 227 | 228 |
| decide (approve_payment) | `e30436a6f60fb6882b3deda10f6f644a4f5d5ffac06b94d93233de58b0cbcda9` | 228 | 229 |

Both registered idempotently via `POST /v1/digest` and confirmed at:
- `GET https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>` → HTTP 200

---

## Full output

```
─── Step 1 — seal fyi capsule (tool call) ─────────────────────────────
  capsule_id  : 56a0c398335c624cb7271108d0d9c8cad6b8912238c625985d9e3c36af7e61d5
  action_type : fyi
  verdict     : executed
  verify().ok : True

─── Step 2 — anchor fyi capsule → POST /v1/digest ─────────────────────
  POST /v1/digest                  HTTP 200
  entry_hash                       : 45b8a3d9dac1c654fc93f00a17fa851b9e9302f4793e6acb506b1cd836c3d7a8
  leaf_index                       : 227
  tree_size                        : 228

  GET /v1/inclusion/<fyi_id>       HTTP 200
  leaf_index                       : 227
  tree_size                        : 228
  root_hash                        : 9ffa89557318dcf3623d37eb5b7cf9873de6bf96dbf7a1043d484d1211ccddcc

  GET /anchor/inclusion-proof-ct   HTTP 200
  audit_path                       : ['a9ec55119f452659387ce995d01b3888ede32cc0284aa3c4de91c3ffa5852574', 'e85d768d3365235227182cccae66ddc8fe74d6dc5c6361743e46d0d1c58e8550', '7eb6f50d7f7b9dafd3f536a00cef3f17a4e7c54f8b55d15d8498c863df432f46', '85b3ea14211229865084fe06ce82926ef717517fc5ab71596333d491d06347e5', 'ad7e2f78c7a7b5d0435914d72f285caf83155d3300c3833ba18cd4ce26970c8f']

  verify_receipt (offline)         : ok=True

─── Step 3 — seal decide capsule (HITL approval) ──────────────────────
  capsule_id     : e30436a6f60fb6882b3deda10f6f644a4f5d5ffac06b94d93233de58b0cbcda9
  action_type    : decide
  verdict        : executed
  human_disposed : True
  decision       : accept
  chained to     : 56a0c398335c624cb7271108d0d9c8cad6b8912238c625985d9e3c36af7e61d5
  verify().ok    : True

─── Step 4 — anchor decide capsule → POST /v1/digest ──────────────────
  POST /v1/digest                  HTTP 200
  entry_hash                       : fb78f8993d4ed064a64496bce4f9eaf9010766f2dae384141dedb4c9ba1e29ce
  leaf_index                       : 228
  tree_size                        : 229

  GET /v1/inclusion/<decide_id>    HTTP 200
  leaf_index                       : 228
  tree_size                        : 229
  root_hash                        : 215f2f3cbf29368cc426b96781d39a9994f947fa2ba1e583dc832bf63c2254bb

  GET /anchor/inclusion-proof-ct   HTTP 200
  audit_path                       : ['f4d824e1b16015134e92cde6c1975094423770e042c81f9459c1c8592e221378', '7eb6f50d7f7b9dafd3f536a00cef3f17a4e7c54f8b55d15d8498c863df432f46', '85b3ea14211229865084fe06ce82926ef717517fc5ab71596333d491d06347e5', 'ad7e2f78c7a7b5d0435914d72f285caf83155d3300c3833ba18cd4ce26970c8f']

  verify_receipt (offline)         : ok=True

─── Summary ───────────────────────────────────────────────────────────
  fyi    capsule_id : 56a0c398335c624cb7271108d0d9c8cad6b8912238c625985d9e3c36af7e61d5
         leaf_index : 227   tree_size : 228
         /v1/inclusion/<id> : HTTP 200
         verify().ok: True   receipt ok: True

  decide capsule_id : e30436a6f60fb6882b3deda10f6f644a4f5d5ffac06b94d93233de58b0cbcda9
         leaf_index : 228   tree_size : 229
         /v1/inclusion/<id> : HTTP 200
         verify().ok: True   receipt ok: True

  All checks PASS.
```

---

## What the evidence means

**POST /v1/digest** (`HTTP 200`) registers the capsule_id (a SHA-256 content address) on the
public SCITT transparency log. The endpoint is idempotent — the same capsule_id always maps to
the same `leaf_index` and `entry_hash`.

**entry_hash** = SHA-256 of the raw capsule_id bytes. This is the offline-verify contract:
any party with the capsule_id can independently compute `sha256(bytes.fromhex(capsule_id))` and
compare it to what the anchor returned.

**GET /v1/inclusion/\<capsule_id\>** (`HTTP 200`) is the convenience lookup: given a
`capsule_id`, returns `leaf_index`, `tree_size`, `root_hash`, `audit_path`, and the signed
COSE receipt in one call. This is what the manager can curl to confirm registration.

**GET /anchor/inclusion-proof-ct** returns the RFC 6962 Merkle audit path for the given
`leaf_index` and `tree_size`. The `audit_path` hashes can be walked up to reproduce the
`root_hash` independently.

**verify_receipt (offline)** calls `scitt_cose.verify_receipt(receipt_bytes,
leaf_entry_hex=entry_hash, log_public_key_pem=pem)`. The Ed25519 log public key is fetched from
`/.well-known/did.json`. A `True` result means the COSE receipt is cryptographically valid and
the entry_hash is committed in the signed Merkle tree — no trust in the anchor server required.

**Decide chains to fyi** via `chain.parent_capsule_id` — the decide capsule's chain field carries
the fyi capsule_id, so a verifier can follow the full sequence: tool call → human approval → both
anchored, both verified.
