# Artifact storage

Optional persistence packages for capsule-emit-go. Root emit APIs retain their
existing storage-free behavior.

## Storage boundary

- Two peer backends implement the same `artifact.Store` contract: `artifact/mysql`
  (MySQL 8.4/InnoDB) and `artifact/sqlite` (modernc, pure-Go, single file). Neither
  is the default; the caller selects one. Each stores exact sealed Capsule bytes, a
  Producer Envelope, and associated originals. Neither seals, appends to CLL, or
  publishes checkpoints.
- `capsule_store_capsules` holds immutable Capsule/envelope bytes and an inventory
  checksum. `capsule_store_artifacts` holds named originals and purge tombstones.
- Artifacts normally use `payload` and optional `agent_output`. Explicit digest
  bindings also support effect request/response preimages. The current AAC wire
  name for a payload commitment is `agent_input_digest`; storage does not rename it.
- An attachment with no digest binding is NOT authenticated by the Capsule.
  Exact-byte SHA-256 checksums detect storage corruption, not producer intent.
- Reads verify Capsule identity, Producer Envelope, configured trusted signer,
  inventory, and every retained bound preimage using the upstream JCS algorithm.
- Namespace is a storage boundary, not a CLL log ID. Multiple logs can reference
  the same stored Capsule. Separate namespaces require separate reader policy.

## Use from Go

This complete function receives deployment configuration from its caller; it
does not assume a global profile, embed credentials, or connect at import time.
The caller must keep the record's originals separate from the sealed Capsule.

```go
package application

import (
    "context"
    "crypto/ed25519"
    "database/sql"
    "errors"

    "github.com/action-state-group/capsule-emit-go/artifact"
    mysqlstore "github.com/action-state-group/capsule-emit-go/artifact/mysql"
    _ "github.com/go-sql-driver/mysql"
)

func Persist(ctx context.Context, dsn, namespace string,
    trustedKey ed25519.PublicKey, record artifact.Record) (err error) {
    db, err := sql.Open("mysql", dsn)
    if err != nil { return err }
    defer func() { err = errors.Join(err, db.Close()) }()
    store, err := mysqlstore.New(db, namespace, []ed25519.PublicKey{trustedKey})
    if err != nil { return err }
    // Provision tables once during deployment, not inside a business transaction.
    if err = store.Init(ctx); err != nil { return err }
    return store.Put(ctx, record)
}
```

Construct each original as `artifact.Artifact{Name: "payload", Content: original,
Binding: artifact.PayloadDigest, State: artifact.Present}`. An absent output needs
no artifact. Use `NeverRetained` only when non-retention is known, not as a fallback
for a failed lookup. `Purged` is created exclusively by `Purge`.

`PutTx` and `GetTx` join a caller-owned transaction on the same database, allowing
atomic artifact writes and application workflow/outbox updates. Roll back the
whole transaction on any error. No CLL append belongs inside this SQL transaction
unless its backend explicitly supports that same transaction contract.

`Put` retries an InnoDB deadlock at most four times after the first attempt, with
context-aware bounded backoff. Each attempt owns and rolls back its SQL transaction.
`PutTx` never retries: the caller decides how to replay its entire transaction.

The SQLite backend (`artifact/sqlite`) exposes the identical
`New`/`Init`/`Put`/`PutTx`/`Get`/`GetTx`/`Purge` API and the same immutability,
idempotent-retry, conflict, and fail-closed-read semantics. Open it with the
`sqlite` driver, `PRAGMA foreign_keys=ON`, and a single open connection so writes
serialize; it retries transient `SQLITE_BUSY`/`SQLITE_LOCKED` in place of an InnoDB
deadlock. It stores the same two tables in one local file, which may also hold a
cll-go SQLite log in its own tables.

## Lifecycle and operations

Writes are immutable and byte-identical retries are idempotent. Changed bytes,
bindings, names, or retention declarations return `ErrConflict`. Reads return
`ErrNotFound` for unknown IDs and fail closed on digest/signature/inventory errors.
`Purge` removes all associated originals while preserving commitments and
tombstones; retries cannot resurrect them. It cannot erase backups or exports.
Purged/never-retained originals are unavailable, not digest-verified.

`Purge` locks and checks the namespace-scoped Capsule row, but deliberately does
not require successful inventory, payload, or signer verification before erasure.
Corrupt records and records whose signer is no longer trusted can still have
their surviving originals removed. This does not repair corruption: `Get` and
`GetTx` continue to fail closed. Database access and namespace authorization must
therefore authorize erasure independently of the configured signer allowlist.

SDK inserts explicitly write UTC `created_at`, matching UTC `purged_at` even on
non-UTC MySQL sessions. Table defaults are unchanged; direct SQL writers must
choose their timestamp policy explicitly.

The v1 limits are 64 artifacts, 65,535 bytes per Producer Envelope (the v1
schema's `BLOB` bound), and 8 MiB total Capsule/envelope/original bytes per record.
Configure MySQL `max_allowed_packet` above that total limit. Retention scheduling,
backup policy, encryption at rest, connection TLS, credentials, and namespace
authorization are deployment responsibilities. Namespace filtering is not a
replacement for DB access control. Metadata/tombstones remain until an explicit
future retention policy removes the collection; no automatic pruning runs.

`Init` only provisions the v1 tables and does not alter preexisting schema.
Schema upgrades need explicit migrations. Producer key rotation is supported by
configuring an allowlist containing both retained historical and current keys.

No application-specific schema migrations, field-level selective disclosure,
profile management, or CLI commands are included. The root emit package and the
`artifact` interface import no storage driver. Importing `artifact/mysql` links the
MySQL driver; importing `artifact/sqlite` links the pure-Go SQLite driver. An
application links only the backend it chooses.

## Tests

```sh
go test ./...
```

Generic MySQL integration tests opt in through `CAPSULE_STORAGE_TEST_DSN` and
accept only TCP `127.0.0.1` and database `capsule_storage_test`. Use a disposable
MySQL 8.4 instance; the tests create namespace-scoped fixtures and intentionally
corrupt their own rows to exercise fail-closed behavior. Without that variable,
MySQL tests skip. SQLite tests need no external server and always run against a
temporary file. Unit tests use testify `assert` and `require`.

Application-specific, one-off migration rehearsal tools are deliberately outside
this repository and are not part of its distribution.
