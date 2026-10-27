# Artifact store conformance vectors

The shared conformance suite for the artifact store contract in
[`docs/artifact-store.md`](../../docs/artifact-store.md). Every artifact store
backend in every language, built in or user-supplied, runs it through its
language's runner (listed in the contract).

`vectors.json` is generated, never edited by hand. The Go test
`go/artifact/artifacttest/generate_test.go` rebuilds it from fixed Ed25519 seeds
and fixed Capsule inputs, so it is reproducible byte for byte; run it with
`UPDATE_ARTIFACT_VECTORS=1` after an intentional change, which also rewrites
`SHA256SUMS`. Each language that ships a runner carries a byte-identical copy,
and its tests fail if the copy drifts from this file.

## File format

| Member | Content |
|---|---|
| `format` | `artifact-store-conformance/v1` |
| `contract` | Path of the contract document |
| `keys` | Named raw Ed25519 public keys (hex): `trusted`, `rotated`, `other` |
| `records` | Named records in the JSONL wire shape (snake_case keys, standard base64 bytes) |
| `checksums` | `{record, storage_checksum}`: the expected storage checksum of a named record |
| `lines` | `{record, line}`: the exact JSONL line of a named record |
| `index_rows` | `{record, paths, stored, ts, entries}`: what the [index extension](../../docs/artifact-store.md#optional-index-extension) stores for a named record under the declared `paths`: the stored declaration, the record's time in UTC microseconds as a decimal string (empty without one), and one `{path, value_sha256}` per entry, in any order. SQL index rows written by one language must match these in every other |
| `open` | `{name, namespace, trusted, expect}`: constructor validation, with `trusted` as raw hex keys (some deliberately not 32 bytes) |
| `cases` | Operation sequences, described below |

The `index.*` records differ in `operator`, `developer`, `timestamp` and a
neutral `fixture` extension whose `tags` array exercises list matching; the
index cases declare only those paths.

The `padding*` records are padding records
([contract](../../docs/artifact-store.md#padding-records)) with fixed store
nonces. Each rejected variant recomputes its own `capsule_id` and carries an
envelope over it, so only the rule its name gives fails.

Record names follow a convention: `basic` is an input, `basic.stored` is what
`get` returns after `put basic` (with `content_sha256` filled in), and
`basic.purged` is what `get` returns after `purge`. Other suffixes are input
variants; `synthetic*` records are codec-only and do not verify.

A record whose wire form the language's codec rejects (for example
`basic.unknown_state` decoded into a typed enum) counts as `invalid` when a
step `put`s it.

## Cases

Each case runs on fresh, empty storage (the harness's "new backing"). It opens
every store in `stores` (a name to `{namespace, trusted}`, with `trusted` as key
names), calls `init()` twice on each and checks `namespace`, then runs `steps`
in order:

| `op` | Fields | Action |
|---|---|---|
| `put` | `record` | `put` the named record |
| `get` | `id`, `result` | `get(id)`; on `ok`, compare with the named record |
| `purge` | `id` | `purge(id)` |
| `reopen` | optional `trusted` | Construct the store again over the same storage (with other trusted keys if given) and `init()` it |
| `init_index` | `paths` | `init_index(paths)` ([index extension](../../docs/artifact-store.md#optional-index-extension)) |
| `reindex` | `paths` | `reindex(paths)` |
| `find` | optional `where`, `since`, `after`, `limit`, `continue`; `found`, `more`, `unordered` | `find` with the given query; with `continue`, `after` is the previous `find`'s `next`. On `ok`, the returned IDs must be the Capsule IDs of the records named in `found`, in that order unless `unordered`, and `next` must be non-empty exactly when `more` |

`store` names the target store (default `main`). `expect` is `ok` or one of the
contract's error codes, and the step passes only with exactly that outcome.
Compare records as the contract defines: artifact order is ignored, and absent
content differs from zero-length content.

`requires` lists optional capabilities. A runner skips a case whose
capabilities the backend does not declare:

- `durable`: a reopened store sees earlier writes (all built-in backends except
  `memory`).
- `shared_backing`: stores with different namespaces can share one storage
  (the SQL backends; a `jsonl` file holds one namespace).
- `index`: the store implements the index extension (every built-in backend
  except the legacy ledgers). Go detects it from the store's type; the other
  runners take it from the harness.
- `durable_index`: a reopened store keeps the declared index paths (the SQL
  backends).

Two further capabilities are derived from a case's steps rather than listed,
so that a store with [capability limits](../../docs/artifact-store.md#capability-limits)
can declare a subset: `originals` when the case puts a record that declares any
artifact, and `purge` when it purges with any expected outcome other than
`invalid`. Every built-in backend except the legacy ledgers has both. The
Python (`Harness.originals`, `Harness.purge`) and Rust (`conformance::Limits`,
`run_limited`) runners take them; Go and TypeScript run every case. The
`bare_*` cases use only `empty` and its variants, so such a store still runs
admission, conflict, rejection and fail-closed read checks.

Corruption detection on read (a tampered row or line) depends on the storage
format, so each backend tests it in its own suite rather than here.

## Cross-language interlock

`interlock.json` drives the cross-language interlock
(`.github/scripts/artifact-store-interlock.sh`, run by the `artifact-interlock`
workflow on every pull request that touches artifact code, once per format:
`jsonl`, `sqlite`, `mysql`). It lists the steps of each phase and the trusted
key names per phase (`default` unless a phase has its own). Each language has a
small adapter that runs one phase against one store and prints one line per
step: the step number, op and subject (a record name, or a JSON-encoded literal
ID), the outcome (`ok` or the error code), and for a successful `get` the
StorageChecksum and the record's JSONL line with artifacts sorted by name. A
record the language's codec cannot represent counts as `invalid`.

- `write`: each language writes to its own store (a file, or for MySQL its own
  database), declaring index paths after the first record so both backfill
  and admission index records, and ends with `find` steps. Every report must equal `interlock-write.txt`. The JSONL files, or
  the SQL schemas and rows as `.github/scripts/artifact_store_rows.py` dumps
  them, must be byte-identical and hold five records. The SQL dump includes
  the index declaration, its records in admission order and its entries. Timestamps compare by
  being NULL or in the contract's UTC form.
- `read`, `strict` and `check`: every language works on fresh copies of every
  language's store, all pairs. `read` reports each record, then retries, a
  conflict and purges of retained content. On a second copy, `strict` trusts
  fewer keys, so a record from the other key is `untrusted_signer` but must
  still be purged, and `check` confirms with the default keys that its retained
  payload is gone and its never-retained output unchanged. Every report must
  equal `interlock-<phase>.txt`, every copy must end in the same state, and
  existing SQL rows must keep their exact `created_at` (a retry writes nothing).
  `read` ends by declaring the same index paths (a no-op where the copy kept
  the writer's declaration, a rebuild where it did not) and finding records.
  The MySQL service runs in a non-UTC time zone, so a backend writing local
  time instead of UTC fails the dump's timestamp check.

The workflow also runs negative controls (`INTERLOCK_INJECT=report`, `storage`,
`read`), which corrupt one language's output and must make the run fail.

The goldens come from the Go adapter (`go run ./artifact/internal/interlock`),
whose test keeps them in sync; every other adapter must reproduce them exactly.
Adapters: Go `go/artifact/internal/interlock`, TypeScript
`ts/scripts/artifact-interlock.mjs`, Python
`python/scripts/artifact_interlock.py`, Rust
`rust/capsule-emit-artifact/examples/interlock.rs`. A pull request that adds a
language's backend adds its adapter and the language to the workflow's
`LANGUAGES`.
