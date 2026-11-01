# Store-backed ledger fixtures

Cross-language fixtures for the [store-backed ledger](../../docs/store-backed-ledger.md):
the same appends give the same log values, artifact records and checkpoint
root in Rust and Python.

| File | Written by | Replayed by |
|---|---|---|
| `rust-written.json` | `rust/capsule-emit/tests/store_backed_ledger_parity.rs` | `python/tests/test_store_backed_ledger.py` |
| `python-written.json` | `python/tests/test_store_backed_ledger.py` | `rust/capsule-emit/tests/store_backed_ledger_parity.rs` |

Both are generated from a fixed Ed25519 key (`[7; 32]`) and fixed inputs, and
each writer's test fails when its file is stale. After an intentional change,
rerun that test with `CAPSULE_EMIT_UPDATE_VECTORS=1`.

| Member | Content |
|---|---|
| `trusted` | The artifact store's trusted keys (raw Ed25519 public keys, hex) |
| `namespace` | The artifact store namespace |
| `steps` | `{op, line, statement}`: `append` or `append_padding`, the line as JSON, its signed statement (hex) |
| `entries` | The log values after the steps, in order (hex) |
| `records` | Each entry's record as its artifact JSONL wire line |
| `checkpoint` | The checkpoint over every entry: `mmr_size` and `root` (Rust's file also has `leaf_count`) |
