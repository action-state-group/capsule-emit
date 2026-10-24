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
| `ledger` | the durable local ledger, with caller-defined indexes: `capsules.jsonl` + `signed-statements/`, or a CLL entry log plus an artifact store |
| `checkpoint` | signed checkpoints on a cadence, optional witness registration; kept beside `capsules.jsonl` or in a CLL backend's state |
| `legacy_ledger` | an existing `capsules.jsonl` ledger as an artifact store and a CLL entry log, for readers |
| `padding` | padding records, so a checkpoint's size falls on a bucket boundary |
| `anchor` | an optional SCITT transparency service client |
| `verify` | offline verification: the reference verifier's Class 1 checks (`agent_action_capsule::verify`, including the id and chain parent), then the signed statement's signature and its binding to the capsule |
| `sequence` | per-counterparty sequence numbers and the gap check |

The `verify_capsule` binary verifies one capsule and statement offline.

## Keeping the ledger in SQLite (or another standard backend)

`Ledger::open(dir)` keeps the flat layout. `Ledger::open_stores(log,
artifacts)` keeps the same ledger in the two standard storage layers: a CLL
entry log (`cll::log` from `checkpointed-local-log`: memory, jsonl, sqlite or
mysql) holding each line's `capsule_id` in order, and a
[`capsule-emit-artifact`](../capsule-emit-artifact) store holding each line as
a record with its producer envelope and signed statement.
`CheckpointState::load_log(log, log_id, cfg, signers)` keeps the checkpoints in
the same CLL backend's state, where an Evidence Bundle producer
(`agent_action_capsule::bundle::proof_source_from_cll`) reads them. Every
other method works as on the flat layout, and the same sealing sequence gives
the same capsule IDs and checkpoint roots on both.

With these dependencies. Neither `open_stores` nor `load_log` is in a published
`capsule-emit` yet (0.0.8 and earlier lack them), and `capsule-emit-artifact`
is not on crates.io yet; until the release that ships them, use both from this
repository:

```toml
capsule-emit = { git = "https://github.com/action-state-group/capsule-emit" }
cll = { package = "checkpointed-local-log", version = "0.3.1", features = ["sqlite"] }
capsule-emit-artifact = { git = "https://github.com/action-state-group/capsule-emit", features = ["sqlite"] }
rusqlite = "0.40"
```

```rust
use std::sync::Arc;
use capsule_emit::checkpoint::CheckpointState;
use capsule_emit::ledger::Ledger;
use capsule_emit_artifact::Store; // for `init`

let db = "ledger.db";
cll::log::sqlite::SqliteStore::init(db, "my-log")?;
let log = Arc::new(cll::log::sqlite::SqliteStore::open(db, "my-log")?);
let artifacts = capsule_emit_artifact::sqlite::SqliteStore::new(
    rusqlite::Connection::open(db)?, "ledger", [node_public_key])?;
artifacts.init()?;
let (mut ledger, _) = Ledger::open_stores(log.clone(), Arc::new(artifacts))?;
let (mut checkpoints, _) = CheckpointState::load_log(log, "my-log", cfg, &[&signing_key])?;
```

What the store layers provide and what `Ledger` checks is in the `ledger`
module documentation. In short: records need a producer envelope from a key
the artifact store trusts; never purge a ledger record (its signed statement
goes with it, and reopening then fails); and one `Ledger` writes a log while
one `CheckpointState` checkpoints it.

`load_log` registers checkpoints with witnesses too. Each checkpoint gets a
witness row per URL in `cfg.witness_urls` in the CLL backend, written with the
checkpoint. The clock legs (`tick`, `reconnect`, `checkpoint_on_shutdown`) and
`retry_pending_witnesses` send each witness its checkpoints in order, and the
receipts are kept in those rows. There is no `checkpoints.jsonl` or
`witness-backfill.jsonl` on this layout. A witness URL is used as the row's
key, so it must be 1 to 191 bytes of `[A-Za-z0-9._:/-]`. A witnessed log
moving from the flat layout must seed the CLL state with its last checkpoint
or start a new log id; otherwise its witnesses refuse the first checkpoint.
Each receipt reads back with the value the witness issued, and a receipt
that a CLL row cannot store holds that witness. `last_checkpoint().witnesses`
lists receipts in `witness_urls` order and only for configured witnesses,
where the flat layout uses arrival order. On SQLite the cost of each
checkpoint still grows with the log's history
([checkpointed-local-log#92](https://github.com/action-state-group/checkpointed-local-log/issues/92)).

Anything a deployment adds beyond the core capsule members goes in
`CapsuleInput::compute_attestation_extensions`: committed into `capsule_id`
in the order given, never interpreted by this crate.

## Verification

This crate produces records; agent-action-capsule verifies them.
`verify::verify_offline` takes its Class 1 verdict from
`agent_action_capsule::verify::verify` and adds only the COSE_Sign1 checks on
the signed statement this crate writes. Read record JSON with
`jcs::decode_record`, not `serde_json`, so it decodes as it does in the
reference (see the `jcs` module).

## Tests

```bash
cargo test
```

`tests/jcs_vectors.rs` runs the pinned conformance vectors in
`tests/vectors/` (see `tests/vectors/SOURCES.md`) through this crate's
canonicalization. `tests/verify_class1.rs` checks that `verify_offline`
refuses signed records the reference verifier refuses, and says which check
did. The cross-language tests
(`cross_language_conformance`, `chain_ledger_conformance`,
`checkpoint_invariance`, `anchor_conformance`) are ignored by default and
need the Python reference installed; each file's header gives the command.

## License

Apache-2.0. The vendored test vectors under `tests/vectors/aac-capsule/` are
BSD-3-Clause; their license travels with them.
