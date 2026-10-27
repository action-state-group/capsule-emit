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
| `produce` | the Go module's producing calls (see below); writes nothing |
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

## Producing Capsules without a ledger (`produce`)

`produce` offers the same calls as the Go module and the TypeScript package,
and returns the same bytes, with one limit: JSON nesting is capped at 128
levels (Go accepts 1000), so Rust refuses an input Go accepts at 129 to 1000
levels, counting the Capsule object itself. `digest_json` writes native
floats as Go does: an integral value below 1e21 as an integer (the
shortest digits that round-trip at the float's own width), a float map key
as the float's own text (`-0`, `1.5`, `1e+21`), NaN and infinities
refused. A `serde_json::Value` number keeps its text, so
`json!(2.0)` is a float, as Go's `json.Number("2.0")` is. Nothing in it writes: the caller supplies the
action id and timestamp and keeps the Capsule and envelope it gets back.

| Call | What |
| --- | --- |
| `build(input)` | validate an `Input`, derive assurance, compute `capsule_id`, return canonical bytes (`BuiltPayload`) |
| `received(input, bytes, type)` / `carry(input, bytes)` | bind an opaque foreign artifact by the SHA-256 of its exact bytes |
| `who`, `can`, `did`, `audit` | assign an existing Capsule to a composition slot |
| `build_composition(input, members)` | bind existing Capsules into slots, in slot order |
| `digest_json(value)` | AAC JSON-DIGEST (SHA-256 over JCS) of a serializable value |
| `sign(built, identity)` | one Producer Envelope over a built Capsule's id |
| `seal(SealInput)` | digest the payload and output, build, sign; returns `Sealed` |
| `verify_capsule(bytes)` | strict decode plus AAC Class 1 |
| `verify_envelope(capsule_id, bytes)` | authenticate a Producer Envelope |
| `decode_payload(bytes)` | strict JSON decode of Capsule bytes |
| `SigningIdentity` | `from_ed25519(key)`, or `new(signer, public_key)` for a KMS or HSM signer |

Go names map to Rust as snake_case, except: `NewEd25519SigningIdentity` is
`SigningIdentity::from_ed25519`, `NewSigningIdentity` is `SigningIdentity::new`,
and Go's `Result` is `Sealed`.

```rust
use capsule_emit::produce::{self, Input, SealInput, SigningIdentity};

let sealed = produce::seal(SealInput {
    capsule: Input { action_id, action_type: "fyi".into(), operator, developer,
                     timestamp: Some(now), ..Input::default() },
    payload: Some(serde_json::json!({"ticket": 105})),
    agent_output: None,
    model: None,
    runtime: String::new(),
    members: None,
    identity: SigningIdentity::from_ed25519(signing_key),
})?;
// sealed.capsule_id, sealed.payload (JCS bytes), sealed.envelope (COSE_Sign1)
```

`tests/produce_interop.rs` replays the Go module's `format4-interop-v05` and
`produce-cross-check` packs and compares every Capsule, envelope, digest and
timestamp byte for byte, and each refusal's error text (for one, only the
part before the JSON decoder's own message).

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
