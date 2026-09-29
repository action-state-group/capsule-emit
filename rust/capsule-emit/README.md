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
| `structure` | the Class 1 checks on a capsule's own bytes (§6, no store): the reference verifier's gating checks |
| `verify` | offline verification (the `structure` checks, id, signature, chain parent) |
| `sequence` | per-counterparty sequence numbers and the gap check |

The `verify_capsule` binary verifies one capsule and statement offline.

Anything a deployment adds beyond the core capsule members goes in
`CapsuleInput::compute_attestation_extensions`: committed into `capsule_id`
in the order given, never interpreted by this crate.

## Differences from the reference verifier

`structure` gives the reference verifier's (`agent_action_capsule.verify`)
verdict and findings on every conformance vector. On inputs outside them,
three differences are known, each pinned by a test in
`tests/structure_vectors.rs`:

- **`-0`** parses as a float here (an integer zero in Python), so a record
  carrying it is refused `float_in_digest_field`.
- **An integer above `u64::MAX`** also parses as a float here: refused
  `float_in_digest_field`, where the reference says
  `unsafe_integer_in_digest_field`. Both refuse the record.
- **A list or object where the reference looks a value up in a closed set**
  (`disposition.approver`, `verdict_class` or `decision`; `effect.type`,
  `effect_attestation` or `irreversibility_class`; `assurance.effect_mode`;
  `provenance_mode.mode`; `chain.relation`) makes the reference (v0.6.0, the
  version the vectors are pinned to) fail with `verifier_internal_error`,
  which refuses the record. Here the closed enums refuse it cleanly
  (`approver_invalid`, `provenance_mode_invalid`) and the registry-only
  fields are not judged, as for any other unseeded value. The reference fix
  ([agent-action-capsule#147](https://github.com/action-state-group/agent-action-capsule/pull/147))
  gives these same answers.

Also by design: at most 64 float and 64 unsafe-integer findings are
reported (the reference lists every one), and a finding quotes at most 64
characters of the record. Neither changes a verdict.

## Tests

```bash
cargo test
```

`tests/jcs_vectors.rs` and `tests/structure_vectors.rs` run the pinned
conformance vectors in `tests/vectors/` (see `tests/vectors/SOURCES.md`):
every Class 1 capsule and provenance-mode case gets the reference verifier's
verdict and error and warning findings. `tests/structure_bounds.rs` checks,
with a counting allocator, that hostile shapes (a 250,000-element array
under a 500 KB key) are checked in memory bounded by a few copies of the
record. The cross-language tests
(`cross_language_conformance`, `chain_ledger_conformance`,
`checkpoint_invariance`, `anchor_conformance`) are ignored by default and
need the Python reference installed; each file's header gives the command.

## License

Apache-2.0. The vendored test vectors under `tests/vectors/aac-capsule/` are
BSD-3-Clause; their license travels with them.
