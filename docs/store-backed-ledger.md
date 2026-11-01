# The store-backed ledger

A store-backed ledger keeps capsule-emit's ledger in the two standard storage
layers instead of a ledger file:

- a CLL entry log (checkpointed-local-log's `docs/backend-contract.md`: JSONL,
  memory, SQLite or MySQL) holding each line's 32-byte `capsule_id`, in order;
- an artifact store ([artifact-store.md](artifact-store.md)) holding each line
  as a record.

| Language | Open it with |
|---|---|
| Rust | `capsule_emit::ledger::Ledger::open_stores` / `open_stores_with_indexer` |
| Python | `capsule_emit.store_backed_ledger.Ledger.open_stores` / `open_stores_with_indexer` |

Both write the same bytes for the same appends, so a log written in one
language opens in the other. The pinned fixtures in
[`test-vectors/store-backed-ledger/`](../test-vectors/store-backed-ledger/README.md)
check this in both directions.

## A line as a record

| Record member | Value |
|---|---|
| `capsule_id` | The line's `capsule_id` |
| `capsule` | The line without `signature` and `key_id`, as serde_json's compact JSON: member order kept, non-ASCII written as UTF-8, only `"`, `\` and control characters escaped |
| `producer_envelope` | The envelope `signature` carries (hex on the line). `key_id` must be exactly its signer in lowercase hex, and `signature` lowercase hex, so the record reads back as the line |
| `artifacts` | One unbound `signed-statement`, `present`: a COSE_Sign1 whose payload is the whole line |

Reading a record back restores `signature` and `key_id` from the envelope.
Padding records ([Evidence Layer -00 §12.1](artifact-store.md#padding-records))
are lines too: in the log, never the chain head, never indexed.

## Writes and opening

A record is `put` first and its log entry appended second, so the entry is
the commit point: a crash between the two leaves a record no entry names,
and the retry's identical `put` is accepted. Before writing, an append runs
the checks an open runs on every line:

- the stored `capsule_id` is the JSON-DIGEST of the line without
  `capsule_id`, `signature` and `key_id`;
- a padding record has exactly its members and a 64-hex `store_nonce`;
- a chained record's `chain.parent_capsule_id` is the head (see below);
- the signed statement parses, names this `capsule_id`, and verifies under
  `key_id`.

A log entry with no record, a record the store refuses, a broken chain or a
bad statement fails the open. Never purge a ledger record: purge erases the
signed statement, and the next open refuses the log.

## Two switches (Python)

A Python flat ledger (`ledger.jsonl`) is not one chain, and has no signed
statements. Two switches let a store-backed ledger hold its records; both are
on by default, which is Rust's behavior:

- `linear`: every chained record's parent is the head. `seal()` chains only
  on `confirms=`, and a bilateral half confirms the other party's capsule, so
  `linear=False` drops the rule;
- `require_statements`: every record has its `signed-statement`.
  `require_statements=False` admits a record with no such attachment at all. A
  purged statement is refused either way, and `append` always takes one.

## Importing a flat Python ledger

`capsule_emit.store_backed_ledger.import_flat(source, log, artifacts)` copies
a flat Python ledger (a `ledger.jsonl` or a ledger store directory) into a
store-backed one opened with both switches off:

- every record is `put` exactly as `capsule_emit.legacy_ledger.LegacyLedger`
  returns it, and appended in order. Nothing is re-signed;
- checkpoint stamps and witness backfills are the flat ledger's checkpoint
  machinery, not records, and are not copied. The new log is checkpointed on
  its CLL backend; keep the flat file, which still holds the earlier
  checkpoints and receipts;
- a line the file repeats under one `capsule_id` is copied once; a different line under the same `capsule_id` refuses the import;
- any other line no store record can keep (an unsigned `log()` entry, a
  disclosure record, a capsule that fails Class 1) refuses the whole import
  before anything is written, naming its lines;
- it is re-runnable: an entry already in the log must hold the byte-identical
  record, and a record whose entry a crash never appended is `put` again.

A signer the artifact store does not trust stops the import at that record;
run it again once the store trusts the key.

## Checkpoints

Checkpoint state lives on the same CLL backend as the entries. In Rust,
`CheckpointState::load_log`; in Python, CLL's
`cll.checkpoint.runner.CheckpointRunner`. For a host log on an EvidenceBook
shelf, the shelf's cadence. The same entries give the same MMR root in both.

## One writer

One `Ledger` writes a log: CLL `append` does not compare parents, so two
writers could each extend the same head. The Python `Ledger` serializes its
own appends and refuses one (`StaleLedgerError`) when the log holds entries it
has not seen, which narrows the race but does not close it. Cross-process
exclusion is the caller's writer lock (the shelf's, for a host log).
