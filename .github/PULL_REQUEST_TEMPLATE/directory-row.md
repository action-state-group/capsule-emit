## Add or update a row in `witnesses.json`

<!-- Open this template with ?template=directory-row.md on the compare URL. -->

**Name** (as it will appear in the row; your service's host name if you declare no other):

**Endpoint:**

**Binding:** `cll` / `rekor` / `scrapi` (delete two; omit the field for `cll`)

### How a reviewer can check the key

<!-- A URL on the endpoint's own host that serves the public key(s) in this row,
     e.g. /.well-known/did.json or /api/v1/log/publicKey.
     The reviewer fetches it and compares it to key_ids (and public_keys) before merging. -->

## Checklist

- [ ] One row per operator and endpoint; rotated keys are added to the same row's `key_ids`, not a new row
- [ ] Row placed in alphabetical order by `name` (case-insensitive)
- [ ] `key_ids`: an Ed25519 key as its raw 32 bytes in 64 lowercase hex; any other key type as the SHA-256 of its DER SubjectPublicKeyInfo, with the key itself in `public_keys` (base64 DER)
- [ ] `since` is the date the service began issuing receipts at this endpoint (YYYY-MM-DD)
- [ ] A receipt from `endpoint` verifies under the listed key
- [ ] `python -m capsule_emit.witness_directory witnesses.json` prints `ok`
- [ ] `pytest tests/test_witness_directory.py` passes
- [ ] The row carries no rating, score, tier, or claim of compliance (the validator refuses unknown fields)
- [ ] DCO sign-off: `git commit -s`
