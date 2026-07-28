# Dapr Agents demo transcript — real anchor inclusion evidence

Generated: 2026-07-28  
Command: `python3 examples/dapr-agents-capsule/demo.py`  
Anchor: `https://anchor.agentactioncapsule.org` (production)

## Full output

```
─── Step 1 — seal fyi capsule (tool call) ─────────────────────────────
  capsule_id  : 594167fdd62a4adc273fc65542a59ede3382bc0dc06fab604db21c9aa6a8d339
  action_type : fyi
  verdict     : executed
  verify().ok : True

─── Step 2 — anchor fyi capsule → POST /v1/digest ─────────────────────
  POST /v1/digest          HTTP 200
  entry_hash               : bfe330c67067f8419ff1d4bc21f53f84078ceb6bb10025a4cccab6203c17b945
  expected (sha256(id))    : bfe330c67067f8419ff1d4bc21f53f84078ceb6bb10025a4cccab6203c17b945
  leaf_index               : 208
  tree_size                : 209
  entry_hash matches       : True

  GET /anchor/inclusion-proof-ct?leaf_index=208&tree_size=209
                           HTTP 200
  leaf_hash    : b9c4be476ccf1d249be53c5ca9303f8db715cefc9b45c573a59267ec06e1e6e0
  audit_path   : ['8b2928b5b98aba2f41c677d5fbddc414d60d7fb5a1a6db2997a2dc4b88eea5bd',
                  '85b3ea14211229865084fe06ce82926ef717517fc5ab71596333d491d06347e5',
                  'ad7e2f78c7a7b5d0435914d72f285caf83155d3300c3833ba18cd4ce26970c8f']
  root_hash    : 449d17a9b5288d2c3109915c853ef59c50d0f84c211d774e7b442aff10461057

  verify_receipt (scitt-cose offline) : ok=True

─── Step 3 — seal decide capsule (HITL approval) ──────────────────────
  capsule_id     : 7fb500b959a4f7d009c56bd01f56412e10167fe7108063cc73325c25a9fc05da
  action_type    : decide
  verdict        : executed
  human_disposed : True
  decision       : accept
  chained to     : 594167fdd62a4adc273fc65542a59ede3382bc0dc06fab604db21c9aa6a8d339
  verify().ok    : True

─── Step 4 — anchor decide capsule → POST /v1/digest ──────────────────
  POST /v1/digest          HTTP 200
  entry_hash               : bd58faba560b79afad6401cd813fed1f065a446224984c58faac2648de7d25e6
  expected (sha256(id))    : bd58faba560b79afad6401cd813fed1f065a446224984c58faac2648de7d25e6
  leaf_index               : 209
  tree_size                : 210
  entry_hash matches       : True

  GET /anchor/inclusion-proof-ct?leaf_index=209&tree_size=210
                           HTTP 200
  leaf_hash    : 547e1dcb8f8e4de9587e5a7387df866c9781dcceefba10001ea813a30b6a068d
  audit_path   : ['b9c4be476ccf1d249be53c5ca9303f8db715cefc9b45c573a59267ec06e1e6e0',
                  '8b2928b5b98aba2f41c677d5fbddc414d60d7fb5a1a6db2997a2dc4b88eea5bd',
                  '85b3ea14211229865084fe06ce82926ef717517fc5ab71596333d491d06347e5',
                  'ad7e2f78c7a7b5d0435914d72f285caf83155d3300c3833ba18cd4ce26970c8f']
  root_hash    : 3c6e0d9011d1b1a15e9c7272faa0a826689a79929a3ce8fed01724ea3a9b98cf

  verify_receipt (scitt-cose offline) : ok=True

─── Summary ───────────────────────────────────────────────────────────
  fyi    capsule_id : 594167fdd62a4adc273fc65542a59ede3382bc0dc06fab604db21c9aa6a8d339
         leaf_index : 208   tree_size : 209
         verify().ok: True   receipt ok: True

  decide capsule_id : 7fb500b959a4f7d009c56bd01f56412e10167fe7108063cc73325c25a9fc05da
         leaf_index : 209   tree_size : 210
         verify().ok: True   receipt ok: True

  All checks PASS.
```

## Live capsule_ids on anchor.agentactioncapsule.org

| Type | capsule_id | leaf_index | tree_size |
|---|---|---|---|
| fyi (execution record, check_invoice) | `594167fdd62a4adc273fc65542a59ede3382bc0dc06fab604db21c9aa6a8d339` | 208 | 209 |
| decide (HITL decision, approve_payment) | `7fb500b959a4f7d009c56bd01f56412e10167fe7108063cc73325c25a9fc05da` | 209 | 210 |

The decide capsule chains to the fyi capsule via `chain.parent_capsule_id`.

## Inclusion proof verification

For each capsule_id, inclusion is confirmed by:

1. **`POST /v1/digest {"capsule_id": "<id>"}`** — idempotent register; returns
   `entry_hash`, `leaf_index`, `tree_size`, `receipt_b64`.  The `entry_hash`
   must equal `SHA-256(bytes.fromhex(capsule_id))` — the offline-verify contract.

2. **`GET /anchor/inclusion-proof-ct?leaf_index=<N>&tree_size=<M>`** — returns
   the RFC6962 Merkle audit path and root hash confirming leaf position in the tree.

3. **`scitt_cose.verify_receipt(receipt, leaf_entry_hex=entry_hash, log_public_key_pem=pem)`**
   — offline Ed25519 signature verification of the COSE Receipt over the reconstructed
   Merkle root.  Both returned `ok=True`.

All three checks pass for both capsules.

## What this shows

- **Capsule 1 (fyi, leaf_index=208)**: emitted by `@emitter.tool("check_invoice")` —
  records that the agent's tool was called.  `action_type="fyi"` because the adapter
  observes what ran; the upstream LLM decision is not visible at this seam.

- **Capsule 2 (decide, leaf_index=209)**: emitted by `emitter.record_hitl()` —
  records Alice's approval of the invoice payment.  `action_type="decide"`,
  `human_disposed=True`, `decision="accept"`.  Chained to capsule 1 via
  `chain.parent_capsule_id`.

Both capsules are in the same sequential batch (leaf_index 208, 209), verifiably
adjacent in the append-only Merkle tree.
