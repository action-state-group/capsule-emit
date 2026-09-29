# capsule-emit (Rust)

Seal, sign, chain and checkpoint Agent Action Capsule records in Rust.

A capsule is a JSON record of one action an agent took. Its `capsule_id` is
the SHA-256 of its RFC 8785 (JCS) canonical form; it is signed as a
COSE_Sign1 statement, appended to a local ledger that refuses broken chain
links and recovers from torn writes, and covered by signed checkpoints of a
Merkle Mountain Range over the ledger.

This crate is the Rust sibling of the Python [`capsule-emit`](https://pypi.org/project/capsule-emit/)
package in the same repository. Its output verifies against the Python
reference and passes the Agent Action Capsule conformance vectors.

```rust
use capsule_emit::capsule::{seal, CapsuleInput};

let capsule = seal(&input)?; // `capsule["capsule_id"]` is set
```

## What is in it

| Module | What |
| --- | --- |
| `capsule` | `seal` a capsule; `seal_local_record` for a record that cites or observes rather than serving an exchange; the inline producer envelope |
| `jcs` | canonicalization and `capsule_id` |
| `cose` | COSE_Sign1 signed statements and producer envelopes |
| `keys` | Ed25519 keys: generate, load, persist (0600), rotate |
| `ledger` | the durable local ledger, with caller-defined indexes |
| `checkpoint` | signed checkpoints on a cadence, optional witness registration |
| `padding` | padding records, so a checkpoint's size falls on a bucket boundary |
| `anchor` | an optional SCITT transparency service client |
| `verify` | offline verification (id, signature, chain parent) |
| `sequence` | per-counterparty sequence numbers and the gap check |

The `verify_capsule` binary verifies one capsule and statement offline.

Anything a deployment adds beyond the core capsule members goes in
`CapsuleInput::compute_attestation_extensions`: committed into `capsule_id`
in the order given, never interpreted by this crate.

## Tests

```bash
cargo test
```

`tests/jcs_vectors.rs` runs the pinned conformance vectors in
`tests/vectors/` (see `tests/vectors/SOURCES.md`). The cross-language tests
(`cross_language_conformance`, `chain_ledger_conformance`,
`checkpoint_invariance`, `anchor_conformance`) are ignored by default and
need the Python reference installed; each file's header gives the command.

## License

Apache-2.0. The vendored test vectors under `tests/vectors/aac-capsule/` are
BSD-3-Clause; their license travels with them.
