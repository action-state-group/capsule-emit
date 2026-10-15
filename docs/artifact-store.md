# Artifact store contract

This is the single written contract for capsule-emit's optional artifact store
in every language. Each language's interface cites it, and the shared
conformance suite in [`test-vectors/artifact-store/`](../test-vectors/artifact-store/)
checks it. A backend, built in or written by a user, conforms when it passes that
suite.

An artifact store keeps the exact bytes of a sealed Capsule, one Producer
Envelope over its Capsule ID, and the business originals the Capsule commits to
(normally the payload and the agent output). It never seals, signs, appends to a
CLL log or publishes checkpoints. The CLL log records that a Capsule ID exists;
the artifact store keeps what it was. One Capsule can appear in several logs, so a
namespace is a storage scope, never a CLL log ID.

## Record model

| Field | Type | Meaning |
|---|---|---|
| `capsule_id` | 64 lowercase hex characters | The Capsule ID the record is stored under |
| `capsule` | bytes | The exact sealed Capsule (AAC format 4, `canonicalization_id: "jcs"`) |
| `producer_envelope` | bytes | One COSE_Sign1 Producer Envelope over `capsule_id` |
| `artifacts` | list of artifacts | The declared originals, at most one per name |

Each artifact has:

| Field | Type | Meaning |
|---|---|---|
| `name` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | Unique within the record; normally `payload` or `agent_output` |
| `binding` | one digest field, or empty | The Capsule digest this original must match. Empty means the original is NOT authenticated by the Capsule |
| `content` | bytes, or absent | The original. Present exactly when `state` is `present`; zero-length content is valid and distinct from absent |
| `state` | `present`, `purged`, `never_retained` | Availability, not authenticity |
| `content_sha256` | 64 lowercase hex characters, or empty | SHA-256 of the exact `content` bytes. Required for `present` and `purged`; empty for `never_retained` |

The supported bindings are exactly these AAC format-4 JSON-DIGEST fields:

- `model_attestation.compute_attestation.agent_input_digest` (the payload)
- `model_attestation.compute_attestation.agent_output_digest`
- `effect.request_digest`
- `effect.response_digest`

`content_sha256` detects storage corruption. It is not a producer claim. An
omitted artifact is undeclared, not evidence that it was never retained:
`never_retained` must be declared explicitly. `purged` is created only by
`purge`.

## Operations

Names follow each language's conventions (`Put`/`put`); the argument order and
semantics are the same everywhere.

| Operation | Contract |
|---|---|
| construct `(storage, namespace, trusted_keys)` | Validates configuration and writes nothing: no schema changes, no files created. `namespace` must match the artifact-name pattern above; `trusted_keys` must be non-empty and each key a raw 32-byte Ed25519 public key, else `invalid`. A file backend (`jsonl`) may read existing storage to build its index, and reports a read failure as a backend error |
| `namespace` | Returns the namespace the store was constructed with |
| `init()` | Provisions storage (tables, file). Idempotent; never alters existing schema or data. Run during provisioning, not inside an application transaction |
| `put(record)` | Admits an immutable record (see Admission) |
| `get(id)` | Returns the stored record after verifying it (see Reads) |
| `purge(id)` | Erases the originals of a stored record (see Retention and purge) |

Constructors stay per backend, because each takes its own storage handle.

### Admission (`put`)

`put` runs these steps in order and stops at the first failure:

1. **Prepare.** Copy the caller's buffers. Reject more than 64 artifacts or a
   Producer Envelope over 65,535 bytes (`invalid`). Then check each artifact
   in record order: a `purged` state is rejected (`purged`); for a `present`
   artifact, compute `content_sha256` over its content (none counts as
   zero-length) and reject a different caller-supplied value (`corrupt`). The
   first failing artifact decides the code. Finally reject a record whose
   Capsule, envelope and contents together exceed 8 MiB (`invalid`).
2. **Verify** the prepared record with the store's trusted keys (see
   Verification).
3. **Insert or reconcile.** If no record with this `capsule_id` exists in the
   namespace, store it atomically. If one exists, read it with the same
   fail-closed checks as `get` (an error there is returned), then compare
   storage checksums:
   a different checksum is `conflict`; an equal checksum whose stored record has
   any `purged` artifact is `purged`; otherwise the call is an idempotent retry
   and succeeds without writing.

Artifact order is not part of a record: a retry that lists the same artifacts
in another order is the same record.

### Verification

Verification authenticates a record; it is run on every `put` and every `get`,
in this order:

1. `capsule_id` matches `^[0-9a-f]{64}$`, else `invalid`.
2. `capsule` decodes as strict JSON (UTF-8, no duplicate keys, no trailing
   data), is AAC format 4 with `canonicalization_id: "jcs"`, passes AAC Class 1
   verification, and its computed Capsule ID equals `capsule_id`. Else `invalid`.
3. `producer_envelope` verifies as an AAC Producer Envelope over `capsule_id`,
   else `invalid`.
4. The envelope's signer is one of the trusted keys, else `untrusted_signer`.
5. For each artifact, in record order:
   - `name` matches the name pattern and is not repeated, else `invalid`.
   - A non-empty `binding` is a supported digest field whose value in the Capsule
     is 64 lowercase hex characters, else `invalid`.
   - `present`: `content` is present, else `invalid`; a non-empty
     `content_sha256` equals SHA-256 of `content`, else `corrupt`; with a
     binding, the AAC JSON-DIGEST of `content` (strict JSON, then JCS, then
     SHA-256) equals the committed digest, and content that is not strict JSON
     also fails, else `digest_mismatch`.
   - `purged`: no `content`, and `content_sha256` is 64 lowercase hex characters,
     else `invalid`.
   - `never_retained`: no `content` and an empty `content_sha256`, else
     `invalid`.
   - Any other state is `invalid`. A record that a language's codec cannot
     represent (for example an unknown state in a typed enum) is also `invalid`.

Verification does not check CLL inclusion, external references or business
correctness.

### Reads (`get`)

`get` rejects a malformed `id` (`invalid`, the same pattern as `capsule_id`)
before any I/O, returns `not_found` when the namespace has no such record, and
otherwise returns the record only if it verifies. Reads fail closed: a backend
with a stored inventory checksum (the SQL backends) returns `corrupt` when the
recomputed storage checksum differs, before verification. `get` never invents a
retention state for an artifact it did not store.

Returned artifacts may come back in any order; compare them by name.

### Retention and purge

`purge(id)` rejects a malformed `id` (`invalid`) and returns `not_found` when the
namespace has no such record. Otherwise it changes every `present` artifact to
`purged`, drops its `content` and keeps its `content_sha256`. `purged` and
`never_retained` artifacts are unchanged. The Capsule, envelope, inventory and
bindings stay, so the storage checksum is unchanged.

- `purge` is idempotent.
- `purge` needs only the record's existence in the namespace. It does not verify
  integrity or the signer, so records that fail verification or the storage
  checksum, and records whose signer is no longer trusted, can still have their
  originals erased. Two `jsonl` exceptions: a line whose `capsule_id` can still
  be read but which no longer decodes as a record cannot be rewritten, so purge
  returns `corrupt`; a line too damaged to yield its `capsule_id` is not indexed
  when the file is opened, so `get` and `purge` report `not_found` and its bytes
  stay in the file until removed by hand. Authorize erasure
  separately from the signer allowlist. `get` keeps failing closed afterwards.
- A purged original cannot come back: a byte-identical `put` after `purge`
  returns `purged`, and a different record returns `conflict`.
- `purge` does not touch CLL entries, backups, replicas or exports.
- Metadata and tombstones stay until a future explicit retention policy removes
  them. No backend prunes automatically.

## Storage checksum

The storage checksum commits the Capsule, envelope and complete artifact
inventory, including each original's SHA-256, and is unchanged by purge. It is
SHA-256 (lowercase hex) over the UTF-8 bytes of this JSON text, with no
whitespace and keys in exactly this order:

```text
{"capsule_id":ID,"capsule":B64,"producer_envelope":B64,"artifacts":[A,...]}
A = {"name":NAME[,"binding":BINDING],"state":STATE[,"content_sha256":HEX]}
```

- `capsule` and `producer_envelope` are standard base64 with padding.
- `artifacts` is sorted by `name` (byte order) and written as `[]` when empty.
- `content` is never included. A `purged` state is written as `present`.
- `binding` and `content_sha256` are omitted when empty.

Goldens: `ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9`
(the synthetic two-artifact record) and
`33be5d36fbe3fcd89e5ec0a3ac5feb0ea764fa08053c34d0978c99fc2dfb71f4` (the same
record with no artifacts), pinned in the conformance vectors.

## Error codes

| Code | Meaning |
|---|---|
| `invalid` | Malformed input or configuration, or a record that fails authentication |
| `conflict` | A different record already exists under this Capsule ID |
| `not_found` | No record with this ID in the namespace |
| `purged` | The originals were purged (or the caller tried to write a purged state) |
| `digest_mismatch` | A bound original does not match the Capsule's digest |
| `untrusted_signer` | The Producer Envelope's signer is not trusted |
| `corrupt` | A stored or supplied checksum does not match the bytes |

Each language exposes these codes: Go as sentinel errors (`artifact.ErrInvalid`,
...), TypeScript as `ArtifactError.code`, Python as `ArtifactError.code` on one
subclass per code, Rust as `ArtifactError::code()`. Storage and driver failures
(I/O, lost connections, an uninitialized store) are backend errors outside this
list; a backend must not report them as `not_found`.

## Optional transactional extension

Backends on a database the application also writes to may let `put` and `get`
join a caller-owned transaction, so originals and other application writes
commit atomically:

| Operation | Contract |
|---|---|
| `put_tx(tx, record)` | The `put` steps, inside `tx`. Never commits, never retries, never opens a nested transaction. On any error the caller rolls back the whole transaction |
| `get_tx(tx, id, lock)` | The `get` steps, inside `tx`. With `lock`, the read serializes with concurrent `put_tx` and `purge` on the same record (MySQL `FOR UPDATE`; SQLite serializes writers, so `lock` changes nothing there) |

Callers detect the extension at run time (a Go type assertion to
`artifact.TxStore`, a TypeScript `TxStore` type guard, a Python `TxStore`
protocol check, a Rust `TxStore` trait bound). Only the SQL backends implement
it. `put` outside a transaction may retry a MySQL deadlock on its own
transaction, at most four times with bounded backoff; `put_tx` never does. A
CLL append does not belong in the same transaction unless the CLL backend
supports that transaction too.

## Built-in backends and shared formats

| Backend | Storage | Transactions |
|---|---|---|
| `jsonl` | One file per namespace, one record per line | No |
| `memory` | Process memory, for tests and short-lived tools | No |
| `mysql` | MySQL 8.4 / InnoDB | Yes |
| `sqlite` | One SQLite file, which may also hold a CLL SQLite log in its own tables | Yes |

Data written by any language opens in the others:

- **`jsonl`**: each line is the JSON text of the record, terminated by `\n`, keys
  in the order `capsule_id`, `capsule`, `producer_envelope`, `artifacts`, and
  per artifact `name`, `binding`, `content`, `state`, `content_sha256`. Bytes
  are standard base64 with padding; `binding` and `content_sha256` are omitted
  when empty; `content` is omitted when absent and written as `""` when
  zero-length; an empty inventory is `[]`. The namespace is not stored per line:
  a file holds one namespace. Readers index by `capsule_id`, last line wins. Put
  appends one line, first ending a torn last line (one without `\n`) so the
  new record is not merged into it; purge rewrites the file through a temporary file and an
  atomic rename. Single writer, no locking. The conformance vectors pin exact
  lines.
- **`mysql` and `sqlite`**: the same two tables, `capsule_store_capsules`
  (`namespace`, `capsule_id`, `capsule_bytes`, `producer_envelope`,
  `record_sha256`, `created_at`) and `capsule_store_artifacts` (`namespace`,
  `capsule_id`, `name`, `digest_field`, `content_bytes`, `content_sha256`,
  `retention_state`, `created_at`, `purged_at`), with the DDL in
  [`go/artifact/mysql/schema.sql`](../go/artifact/mysql/schema.sql) and
  [`go/artifact/sqlite/schema.sql`](../go/artifact/sqlite/schema.sql). An
  unbound artifact stores `digest_field` as `''`; `never_retained` stores
  `content_sha256` as `''`. Writers set `created_at` and `purged_at` in UTC
  (MySQL `UTC_TIMESTAMP(6)`, SQLite `strftime('%Y-%m-%dT%H:%M:%fZ','now')`).
- **`memory`** has no persisted format.

## Limits and deployment

The v1 limits are 64 artifacts, 65,535 bytes per Producer Envelope and 8 MiB of
Capsule, envelope and original bytes per record; configure MySQL
`max_allowed_packet` above 8 MiB. `init()` creates only v1 schema; upgrades need
explicit migrations. Retention scheduling, backups, encryption at rest,
connection security, credentials and namespace authorization belong to the
deployment; a namespace filter does not replace database access control. To
rotate producer keys, trust both the retained historical keys and the current
ones.

## Conformance suite

[`test-vectors/artifact-store/vectors.json`](../test-vectors/artifact-store/vectors.json)
holds deterministic signed records, codec goldens and operation sequences with
expected results and error codes. Each language ships a runner that takes a
factory for the backend under test:

| Language | Runner |
|---|---|
| Go | `artifacttest.Run(t, artifacttest.Harness{...})` |
| TypeScript | `runArtifactConformance(harness)` from `@action-state-group/capsule-emit/artifact/conformance` |
| Python | `capsule_emit.artifact.conformance.run(harness)` |
| Rust | `capsule_emit_artifact::conformance::run(&harness)` |

The TypeScript, Python and Rust runners and interfaces, including the
TypeScript `TxStore` and the `lock` argument of TypeScript SQLite `getTx`, land
with those languages' backends (capsule-emit#308); until then only Go runs the
suite.

The vectors' README describes the file format and the optional capabilities a
case may require (`durable`: the backend can be reopened over the same storage;
`shared_backing`: several namespaces can share one storage).
