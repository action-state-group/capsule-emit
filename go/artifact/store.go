package artifact

import (
	"context"
	"database/sql"
)

// Store is the backend-neutral artifact store contract, specified in
// docs/artifact-store.md and checked by the shared conformance suite
// (artifacttest.Run). Constructors stay per backend because each takes its own
// storage handle. Every built-in backend (jsonl, memory, mysql, sqlite)
// implements Store; a user-supplied backend conforms when it passes the suite.
type Store interface {
	// Namespace returns the storage scope the store was constructed with.
	Namespace() string
	// Init provisions storage. It is idempotent and never alters existing data.
	Init(ctx context.Context) error
	// Put admits an immutable record, accepting byte-identical retries.
	Put(ctx context.Context, record Record) error
	// Get returns a stored record after fail-closed verification.
	Get(ctx context.Context, id string) (Record, error)
	// Purge erases a record's present originals, keeping their commitments.
	Purge(ctx context.Context, id string) error
}

// TxStore is the optional transactional extension: Put and Get that join a
// caller-owned SQL transaction. Only the SQL backends implement it; detect it
// with a type assertion:
//
//	if txs, ok := store.(artifact.TxStore); ok { ... }
//
// PutTx never commits or retries; on any error the caller rolls back the whole
// transaction. With lock, GetTx serializes the read with concurrent PutTx and
// Purge on the same record (MySQL FOR UPDATE; SQLite already serializes writers).
type TxStore interface {
	Store
	PutTx(ctx context.Context, tx *sql.Tx, record Record) error
	GetTx(ctx context.Context, tx *sql.Tx, id string, lock bool) (Record, error)
}
