# Test vectors: where they come from

Copied verbatim from the source; not edited here.

| Directory | Source | Pin | License |
| --- | --- | --- | --- |
| `evidence-request/` | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `vectors/evidence-request/` (the draft-mih-agent-evidence-request-00 conformance vectors) | commit `ced858f7df90f8211777c8f20c7a11744f5b7ea2` | BSD-3-Clause: `evidence-request/LICENSE`, the source repository's license, copied verbatim, travels with the copies |
| `scitt-proof-array/` | [scitt-cose](https://github.com/action-state-group/scitt-cose) `test-vectors/v1/` proof-array derivatives | commit `e539fe4d72e6ad70fe443bf2079795322c69a711` | Apache-2.0; see `scitt-proof-array/SOURCES.md` |

`scitt-proof-array/SHA256SUMS` pins every copied file and is checked by
`receipt_vectors.rs` before any vector is read. The Rust CI cargo test step
runs that check.

`evidence-request/SHA256SUMS` is the source's own checksum file, unchanged:
it lists every vector file except `LICENSE` (added here). The test suite
checks every listed file against it before it reads any vector, and CI runs
`shasum -a 256 -c SHA256SUMS` in that directory.

To update: copy the new set unchanged from the source commit, keep
`LICENSE`, and update the pin here.
