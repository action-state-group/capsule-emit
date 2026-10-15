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
| `open` | `{name, namespace, trusted, expect}`: constructor validation, with `trusted` as raw hex keys (some deliberately not 32 bytes) |
| `cases` | Operation sequences, described below |

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

Corruption detection on read (a tampered row or line) depends on the storage
format, so each backend tests it in its own suite rather than here.

## Cross-language interlock

`interlock.json` drives the cross-language interlock
(`.github/scripts/artifact-store-interlock.sh`, run by the
`artifact-interlock` workflow on every pull request that touches artifact
code, once per format: `jsonl`, `sqlite`, `mysql`). Each language has a small
adapter that runs one phase of it against one store and prints one line per
step: the step number, op and subject, the outcome (`ok` or the error code),
and for a successful `get` the StorageChecksum and the record's JSONL line with
artifacts sorted by name.

- `write`: each language writes the sequence to its own store. Every report
  must equal `interlock-write.txt`, and the JSONL files, or the SQL rows as
  dumped by `.github/scripts/artifact_store_rows.py`, must be byte-identical.
- `read`: every language runs the read phase on its own copy of every
  language's store, all pairs: each record's outcome, StorageChecksum and line,
  then retries, a conflict and purges of retained content against that data.
  Every report must equal `interlock-read.txt`, every copy must end in the same
  state, and existing SQL rows must keep their exact `created_at` (a retry
  writes nothing).

`interlock-write.txt` and `interlock-read.txt` come from the Go adapter
(`go run ./artifact/internal/interlock`); every other adapter must reproduce
them exactly. The Go adapter is `go/artifact/internal/interlock`, the
TypeScript one `ts/scripts/artifact-interlock.mjs`. A pull request that adds a
language's backend adds its adapter and adds the language to the workflow's
`LANGUAGES`.
