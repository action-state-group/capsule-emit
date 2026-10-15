# Evidence Layer padding record vectors

Conformance cases for the padding record of
`draft-mih-agent-evidence-layer-00`, Privacy Considerations, "What a
Checkpoint Reveals". A store appends padding records so that a checkpoint's
size falls on a bucket boundary.

## The record

A padding record has `record_type` `padding`, `epistemic_type`
`producer_claim`, and as its only content a fresh random value in the member
`store_nonce`: 32 bytes encoded as 64 lowercase hexadecimal characters. It
carries no `payload_commitments`, no links, no `subject_ref` and no
`principal_ref`.

## The serialization these cases test

The draft fixes the record's members but not its serialization or how a
content-derived `record_id` is computed. These cases test the JSON
serialization the existing padding writers share, the one an Agent Action
Capsule ledger uses:

```json
{"capsule_id": "<64 lowercase hex>", "record_type": "padding",
 "epistemic_type": "producer_claim", "store_nonce": "<64 lowercase hex>"}
```

In it, the record's `record_id` is carried in the member `capsule_id` and is
the Digest (lowercase-hex SHA-256 of `UTF8(JCS(value))`, RFC 8785) of the
record without `capsule_id`. Each case records that preimage's literal JCS
string in `capsule_id_preimage`. A padding record in this serialization has
exactly these four members: `seq` is the substrate's append position and is
not stored in the record, and a padding record has no `links` member, not
even an empty one.

A store that serializes records differently checks its padding records by
building this JSON object from their members and computing `capsule_id` over
it. A record identifier the store derives some other way, or assigns, is
outside these cases. A store that uses this serialization removes members it
keeps beside the record outside the id preimage (for example the Agent
Action Capsule local-only `signature` and `key_id`) before checking.

Every case has `record_type` `padding`. A record with any other
`record_type` is not a padding record, and these cases do not apply to it.

## Cases (`cases.json`)

A checker accepts every `positive` case, recomputing its `capsule_id`, and
rejects every `negative` case. `expected.reason` names the one rule a
negative case breaks, and `expected.member` the member that breaks it. Every
negative case except `neg-capsule-id-mismatch` carries a `capsule_id` that
recomputes over its own members, so its id is not why it fails.

| Reason | Cases |
|---|---|
| (valid) | `pos-padding-a`, `pos-padding-b`, `pos-padding-digits-only` |
| `missing_store_nonce` | `neg-missing-store-nonce` |
| `store_nonce_encoding` | `neg-store-nonce-uppercase`, `-short`, `-long`, `-not-hex`, `-not-string` |
| `extra_member` | `neg-extra-member`, `neg-links`, `neg-payload-commitments`, `neg-subject-ref`, `neg-principal-ref` |
| `epistemic_type` | `neg-epistemic-type-observed-event`, `neg-epistemic-type-missing` |
| `capsule_id_mismatch` | `neg-capsule-id-mismatch` |

The nonces are fixed values derived from a label, so a run is reproducible. A
store draws each nonce from a cryptographically secure random source, for
that record alone; no vector can test freshness.

## Regenerating and checking

```bash
python3 python/scripts/generate_evidence_layer_padding_vectors.py
python -m pytest python/tests/test_evidence_layer_padding_vectors.py
```

The run is deterministic and uses no keys. The test recomputes every
`capsule_id` from the files alone, checks the JCS strings against an
independent RFC 8785 implementation, and applies the rules above to every
case.
