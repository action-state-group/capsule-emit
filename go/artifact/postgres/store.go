// Package postgres implements the artifact Store on PostgreSQL through
// database/sql. Open the *sql.DB with the pgx stdlib driver
// (github.com/jackc/pgx/v5/stdlib, driver name "pgx").
package postgres

import (
	"context"
	"crypto/ed25519"
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/index"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var idPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

//go:embed schema.sql
var schema string

var dialect = index.Dialect{
	TablesExist: `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=current_schema()` +
		` AND table_name IN ('capsule_store_index_paths','capsule_store_index_records','capsule_store_index')`,
	LockShared: ` FOR SHARE`,
	LockUpdate: ` FOR UPDATE`,
	Rebind:     rebind,
	// PostgreSQL has no LastInsertId; RETURNING yields the new seq, and no row
	// back means the record was already indexed.
	InsertRecord: func(ctx context.Context, tx *sql.Tx, namespace, id string, ts any) (int64, bool, error) {
		var seq int64
		err := tx.QueryRowContext(ctx, `INSERT INTO capsule_store_index_records (namespace,capsule_id,ts) VALUES($1,$2,$3)`+
			` ON CONFLICT DO NOTHING RETURNING seq`, namespace, id, ts).Scan(&seq)
		if errors.Is(err, sql.ErrNoRows) {
			return 0, false, nil
		}
		return seq, err == nil, err
	},
}

// rebind rewrites "?" placeholders to $1, $2, ... None of this package's
// statements contains a literal question mark.
func rebind(query string) string {
	var out strings.Builder
	n := 0
	for i := 0; i < len(query); i++ {
		if query[i] == '?' {
			n++
			out.WriteString("$" + strconv.Itoa(n))
			continue
		}
		out.WriteByte(query[i])
	}
	return out.String()
}

var (
	_ artifact.TxStore    = (*Store)(nil)
	_ artifact.IndexStore = (*Store)(nil)
)

// Store is an application-importable SDK, independent of CLI profiles and CLL
// log IDs. The caller owns the DB pool, credentials, TLS, and trusted keys.
type Store struct {
	db        *sql.DB
	namespace string
	trusted   []ed25519.PublicKey
	lookup    *index.SQL
}

// New does not connect, migrate schema, or start background workers.
// A namespace isolates an artifact collection; it must not implicitly be a log ID.
func New(db *sql.DB, namespace string, trusted []ed25519.PublicKey) (*Store, error) {
	if db == nil || !namePattern.MatchString(namespace) || len(trusted) == 0 {
		return nil, artifact.ErrInvalid
	}
	s := &Store{db: db, namespace: namespace, lookup: &index.SQL{Namespace: namespace, Dialect: dialect}}
	for _, key := range trusted {
		if len(key) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("%w: public key size", artifact.ErrInvalid)
		}
		s.trusted = append(s.trusted, append(ed25519.PublicKey(nil), key...))
	}
	return s, nil
}

// Namespace returns the explicit storage scope, independent of CLL coordinates.
func (s *Store) Namespace() string { return s.namespace }

// Init creates v1 tables. Run explicitly during provisioning, not within an
// application transaction. No existing table or
// application data is altered. Future schema upgrades require versioned migrations.
func (s *Store) Init(ctx context.Context) error {
	for _, statement := range strings.Split(schema, ";") {
		if strings.TrimSpace(statement) == "" {
			continue
		}
		if _, err := s.db.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("initialize artifact store: %w", err)
		}
	}
	return nil
}

// Put persists an immutable record atomically, accepting byte-identical retries.
func (s *Store) Put(ctx context.Context, record artifact.Record) (err error) {
	return retryConflict(ctx, func() error { return s.putOnce(ctx, record) })
}

func (s *Store) putOnce(ctx context.Context, record artifact.Record) (err error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return err
	}
	defer finishRollback(tx, &err)
	if err = s.PutTx(ctx, tx, record); err != nil {
		return err
	}
	return tx.Commit()
}

// PutTx joins a caller-owned transaction on this store's database. The caller
// MUST roll back the whole transaction on error; this method never commits or
// hides partial work with a nested transaction. This lets Alchemy atomically
// finalize originals, advance a session head, and queue CLL delivery.
func (s *Store) PutTx(ctx context.Context, tx *sql.Tx, record artifact.Record) error {
	if tx == nil {
		return artifact.ErrInvalid
	}
	prepared, err := artifact.Prepare(record)
	if err != nil {
		return err
	}
	record = prepared
	if _, err := artifact.Verify(record, s.trusted); err != nil {
		return err
	}
	if err := s.lockIndex(ctx, tx, true); err != nil {
		return err
	}
	hash, err := record.StorageChecksum()
	if err != nil {
		return err
	}
	// A failed statement aborts a PostgreSQL transaction, so a duplicate is
	// detected with ON CONFLICT DO NOTHING rather than by catching the error.
	result, err := tx.ExecContext(ctx, `INSERT INTO capsule_store_capsules
		(namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256) VALUES($1,$2,$3,$4,$5)
		ON CONFLICT (namespace,capsule_id) DO NOTHING`,
		s.namespace, record.CapsuleID, record.Capsule, record.ProducerEnvelope, hash)
	if err != nil {
		return err
	}
	inserted, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if inserted == 0 {
		old, readErr := s.read(ctx, tx, record.CapsuleID, true)
		if readErr != nil {
			return readErr
		}
		oldHash, hashErr := old.StorageChecksum()
		if hashErr != nil {
			return hashErr
		}
		if oldHash != hash {
			return artifact.ErrConflict
		}
		for _, a := range old.Artifacts {
			if a.State == artifact.Purged {
				return artifact.ErrPurged
			}
		}
		return nil
	}
	for _, a := range record.Artifacts {
		_, err = tx.ExecContext(ctx, `INSERT INTO capsule_store_artifacts
			(namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state) VALUES($1,$2,$3,$4,$5,$6,$7)`,
			s.namespace, record.CapsuleID, a.Name, string(a.Binding), a.Content, a.ContentSHA256, string(a.State))
		if err != nil {
			return err
		}
	}
	return s.lookup.Admit(ctx, tx, record.CapsuleID, record.Capsule)
}

// Get loads a coherent snapshot and verifies authenticity and original digests.
// Missing rows return artifact.ErrNotFound, never an invented retention status.
func (s *Store) Get(ctx context.Context, id string) (_ artifact.Record, err error) {
	if !idPattern.MatchString(id) {
		return artifact.Record{}, artifact.ErrInvalid
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return artifact.Record{}, err
	}
	defer finishRollback(tx, &err)
	record, err := s.read(ctx, tx, id, false)
	if err != nil {
		return artifact.Record{}, err
	}
	if err = tx.Commit(); err != nil {
		return artifact.Record{}, err
	}
	return record, nil
}

// GetTx reads within a caller transaction. lock serializes this read with
// PutTx/Purge via the Capsule parent row when finalization requires it.
func (s *Store) GetTx(ctx context.Context, tx *sql.Tx, id string, lock bool) (artifact.Record, error) {
	if tx == nil {
		return artifact.Record{}, artifact.ErrInvalid
	}
	return s.read(ctx, tx, id, lock)
}

func (s *Store) read(ctx context.Context, tx *sql.Tx, id string, lock bool) (_ artifact.Record, err error) {
	if !idPattern.MatchString(id) {
		return artifact.Record{}, artifact.ErrInvalid
	}
	record := artifact.Record{CapsuleID: id}
	var expected string
	query := `SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=$1 AND capsule_id=$2`
	if lock {
		query += ` FOR UPDATE`
	}
	err = tx.QueryRowContext(ctx, query, s.namespace, id).Scan(&record.Capsule, &record.ProducerEnvelope, &expected)
	if errors.Is(err, sql.ErrNoRows) {
		return artifact.Record{}, artifact.ErrNotFound
	}
	if err != nil {
		return artifact.Record{}, err
	}
	query = `SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=$1 AND capsule_id=$2 ORDER BY name`
	if lock {
		query += ` FOR UPDATE`
	}
	rows, err := tx.QueryContext(ctx, query, s.namespace, id)
	if err != nil {
		return artifact.Record{}, err
	}
	defer func() { err = errors.Join(err, rows.Close()) }()
	for rows.Next() {
		var a artifact.Artifact
		if err := rows.Scan(&a.Name, &a.Binding, &a.Content, &a.ContentSHA256, &a.State); err != nil {
			return artifact.Record{}, err
		}
		record.Artifacts = append(record.Artifacts, a)
	}
	if err = rows.Err(); err != nil {
		return artifact.Record{}, err
	}
	actual, err := record.StorageChecksum()
	if err != nil {
		return artifact.Record{}, err
	}
	if expected != actual {
		return artifact.Record{}, artifact.ErrCorrupt
	}
	if _, err = artifact.Verify(record, s.trusted); err != nil {
		return artifact.Record{}, err
	}
	return record, nil
}

// Purge deletes business originals, retaining the exact Capsule/envelope,
// inventory and tombstones. It does not remove CLL entries or claim to erase
// database backups, replicas, exports, or application copies.
// Erasure requires only namespace-scoped existence, not integrity or signer
// verification: damaged records must not prevent retention enforcement. Get
// continues to fail closed; purging does not repair a corrupt record.
func (s *Store) Purge(ctx context.Context, id string) (err error) {
	if !idPattern.MatchString(id) {
		return artifact.ErrInvalid
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer finishRollback(tx, &err)
	var existing string
	err = tx.QueryRowContext(ctx, `SELECT capsule_id FROM capsule_store_capsules WHERE namespace=$1 AND capsule_id=$2 FOR UPDATE`, s.namespace, id).Scan(&existing)
	if errors.Is(err, sql.ErrNoRows) {
		return artifact.ErrNotFound
	}
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE capsule_store_artifacts SET content_bytes=NULL,retention_state='purged',purged_at=now()
		WHERE namespace=$1 AND capsule_id=$2 AND retention_state='present'`, s.namespace, id)
	if err != nil {
		return err
	}
	return tx.Commit()
}

func finishRollback(tx *sql.Tx, err *error) {
	rollbackErr := tx.Rollback()
	if rollbackErr != nil && !errors.Is(rollbackErr, sql.ErrTxDone) {
		*err = errors.Join(*err, rollbackErr)
	}
}

// lockIndex orders writes against index declaration for this namespace with a
// transaction-scoped advisory lock: PutTx holds it shared from before its insert
// until the caller's commit, InitIndex and Reindex take it exclusively. A
// PostgreSQL locking read does not see, and does not wait for, a row another
// transaction has inserted but not committed, so the row locks the MySQL
// backend relies on cannot keep a record out of the index here. With this lock
// a declaration and an in-flight Put never overlap: whichever commits first, the
// other then reads the committed result in a fresh READ COMMITTED statement.
// The store opens its own Put, InitIndex, Reindex and Find transactions at
// READ COMMITTED explicitly, whatever the connection default. PutTx requires
// READ COMMITTED for the same reason: a REPEATABLE READ snapshot
// could predate a declaration that committed before the insert.
func (s *Store) lockIndex(ctx context.Context, tx *sql.Tx, shared bool) error {
	if shared {
		var level string
		if err := tx.QueryRowContext(ctx, `SELECT current_setting('transaction_isolation')`).Scan(&level); err != nil {
			return err
		}
		if level != "read committed" {
			return fmt.Errorf("%w: PutTx requires a READ COMMITTED transaction, got %s", artifact.ErrInvalid, level)
		}
	}
	fn := "pg_advisory_xact_lock"
	if shared {
		fn += "_shared"
	}
	_, err := tx.ExecContext(ctx, `SELECT `+fn+`(hashtextextended('capsule_store_index:' || $1::text, 0))`, s.namespace)
	return err
}

// InitIndex provisions the tables as Init does, then declares paths for the
// namespace and indexes its stored records. The same set again does nothing; a
// different set is artifact.ErrConflict.
func (s *Store) InitIndex(ctx context.Context, paths []string) error {
	if _, err := index.NormalizePaths(paths); err != nil {
		return err
	}
	if err := s.Init(ctx); err != nil {
		return err
	}
	return retryConflict(ctx, func() error {
		return s.inTx(ctx, func(tx *sql.Tx) error {
			if err := s.lockIndex(ctx, tx, false); err != nil {
				return err
			}
			return s.lookup.Declare(ctx, tx, paths)
		})
	})
}

// Reindex replaces the namespace's declared paths and rebuilds its index.
func (s *Store) Reindex(ctx context.Context, paths []string) error {
	if _, err := index.NormalizePaths(paths); err != nil {
		return err
	}
	if err := s.Init(ctx); err != nil {
		return err
	}
	return retryConflict(ctx, func() error {
		return s.inTx(ctx, func(tx *sql.Tx) error {
			if err := s.lockIndex(ctx, tx, false); err != nil {
				return err
			}
			return s.lookup.Reindex(ctx, tx, paths)
		})
	})
}

// Find returns one page of IDs matching q in admission order.
func (s *Store) Find(ctx context.Context, q artifact.Query) (page artifact.Page, err error) {
	err = s.inTx(ctx, func(tx *sql.Tx) error {
		page, err = s.lookup.Find(ctx, tx, q)
		return err
	})
	return page, err
}

func (s *Store) inTx(ctx context.Context, run func(*sql.Tx) error) (err error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return err
	}
	defer finishRollback(tx, &err)
	if err = run(tx); err != nil {
		return err
	}
	return tx.Commit()
}
