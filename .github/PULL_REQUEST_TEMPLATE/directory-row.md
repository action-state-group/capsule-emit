## Add or update a row in `witnesses.json`

<!-- Open this template with ?template=directory-row.md on the compare URL. -->

**Array:** `witnesses` / `countersigners` (delete one)

**Operator name** (as it will appear in the row):

**Endpoint:**

### How a reviewer can check the key

<!-- A URL on the endpoint's own host that serves the public key(s) in this row,
     e.g. /.well-known/did.json or /anchor/authority-pubkey. The reviewer fetches it
     and compares it to key_ids before merging. -->

### Countersigners only

- **Statement types issued:** <!-- e.g. countersign/v1 -->
- **Independent of:** <!-- parties you declare no common control with, or "none declared" -->
- **Public statement of what you recompute:** <!-- link: which checks, which results
     they can return, and your registration policy -->

## Checklist

- [ ] One row per operator and endpoint; rotated keys are added to the same row's `key_ids`, not a new row
- [ ] Row placed in alphabetical order by `name` (case-insensitive)
- [ ] `key_ids` are full 32-byte Ed25519 public keys, 64 lowercase hex characters
- [ ] `since` is the date the service began issuing (YYYY-MM-DD)
- [ ] `python -m capsule_emit.witness_directory witnesses.json` prints `ok`
- [ ] `pytest tests/test_witness_directory.py` passes
- [ ] The row carries no rating, score, tier, or claim of compliance (the validator refuses unknown fields)
- [ ] DCO sign-off: `git commit -s`
