# capsule-emit-artifact

An artifact store for Agent Action Capsules: the exact bytes of a sealed
Capsule, one Producer Envelope over its Capsule ID, and the business originals
the Capsule commits to (normally the payload and the agent output), with
retention and purge. It never seals, signs or appends to a CLL log.

The contract is written once for every language in
[`docs/artifact-store.md`](https://github.com/action-state-group/capsule-emit/blob/main/docs/artifact-store.md),
and the Go, TypeScript, Python and Rust stores read each other's JSONL files and
SQL rows.

| Backend | Module | Cargo feature | Transactions |
|---|---|---|---|
| One JSONL file per namespace | `jsonl` | none | no |
| Process memory | `memory` | none | no |
| SQLite (rusqlite, bundled) | `sqlite` | `sqlite` | yes |
| MySQL 8.4 (`mysql` crate) | `mysql` | `mysql` | yes |
| capsule-emit's ledger directory | `legacy` | `legacy` | no |

Without features the crate links no database driver. Every backend implements
`Store` (`namespace`, `init`, `put`, `get`, `purge`); the SQL backends also
implement `TxStore` (`put_tx`, `get_tx` with `lock`). The `mysql` feature uses
the `mysql` crate with no TLS; enable one of its TLS features in your own
manifest when the connection needs it.

```rust
use capsule_emit_artifact::{jsonl::JsonlStore, Store};

let store = JsonlStore::new("artifacts.jsonl", "my-namespace", [trusted_public_key])?;
store.init()?; // creates the file once
store.put(&record)?; // verifies the Capsule, envelope signer and bound originals
let stored = store.get(&record.capsule_id)?;
store.purge(&record.capsule_id)?; // erases originals, keeps their commitments
```

`conformance::run` runs the shared, language-neutral conformance suite against
any `Store`, including one you write; `conformance::run_limited` runs it for a
store with capability limits.

The `legacy` feature puts capsule-emit's existing ledger (`capsules.jsonl` +
`signed-statements/`) behind `Store` and the CLL `cll::log::EntryStore`, so
existing ledgers keep verifying: no originals besides the required
`signed-statement` attachment, no purge, no digest-only append and one writer
([`docs/legacy-ledger.md`](https://github.com/action-state-group/capsule-emit/blob/main/docs/legacy-ledger.md)).

Building this crate in the same build as `capsule-emit` turns on serde_json's
`arbitrary_precision` feature (through `agent-action-capsule`). serde_json
then reads `-0` as the integer 0, as the Go and Python references do, so
`capsule-emit`'s structure check accepts it instead of refusing it as a float.

Licensed under Apache-2.0.
