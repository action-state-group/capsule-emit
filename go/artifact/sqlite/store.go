// Package sqlite implements the artifact Store on SQLite (modernc, pure Go).
// It mirrors the MySQL backend's contract: immutable records, byte-identical
// retry admission, and fail-closed reads that verify signer and original digests.
package sqlite

import (
	"context"
	"crypto/ed25519"
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/index"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var idPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

//go:embed schema.sql
var schema string

var dialect = index.Dialect{
	TablesExist:  `SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('capsule_store_index_paths','capsule_store_index_records','capsule_store_index')`,
	InsertIgnore: `INSERT OR IGNORE INTO`,
}

var (
	_ artifact.TxStore    = (*Store)(nil)
	_ artifact.IndexStore = (*Store)(nil)
)

// Store is an application-importable SDK, independent of CLI profiles and CLL
// log IDs. The caller owns the DB pool (single writer), and trusted keys. The
// same database file may also hold a cll-go SQLite log in separate tables.
type Store struct {
	db        *sql.DB
	namespace string
	trusted   []ed25519.PublicKey
	lookup    *index.SQL
}

// New does not connect, migrate schema, or start background workers. The caller
// must open the *sql.DB with the "sqlite" driver, foreign_keys ON and a single
// open connection so writes serialize. A namespace isolates an artifact
// collection; it must not implicitly be a log ID.
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

// Init creates v1 tables. Run explicitly during provisioning. No existing table
// or application data is altered. Future schema upgrades require versioned
// migrations.
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
// The single open connection with busy_timeout serializes writers, so no
// SQLITE_BUSY retry loop is needed.
func (s *Store) Put(ctx context.Context, record artifact.Record) (err error) {
	tx, err := s.db.BeginTx(ctx, nil)
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
// MUST roll back the whole transaction on error; this method never commits.
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
	hash, err := record.StorageChecksum()
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO capsule_store_capsules
		(namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256) VALUES(?,?,?,?,?)`,
		s.namespace, record.CapsuleID, record.Capsule, record.ProducerEnvelope, hash)
	if err != nil {
		if !constraint(err) {
			return err
		}
		old, readErr := s.read(ctx, tx, record.CapsuleID)
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
			(namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state) VALUES(?,?,?,?,?,?,?)`,
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
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return artifact.Record{}, err
	}
	defer finishRollback(tx, &err)
	record, err := s.read(ctx, tx, id)
	if err != nil {
		return artifact.Record{}, err
	}
	if err = tx.Commit(); err != nil {
		return artifact.Record{}, err
	}
	return record, nil
}

// GetTx reads within a caller transaction. lock is accepted for the shared
// artifact.TxStore signature and changes nothing here: SQLite serializes
// writers, so the read is already consistent with PutTx/Purge.
func (s *Store) GetTx(ctx context.Context, tx *sql.Tx, id string, lock bool) (artifact.Record, error) {
	if tx == nil {
		return artifact.Record{}, artifact.ErrInvalid
	}
	return s.read(ctx, tx, id)
}

func (s *Store) read(ctx context.Context, tx *sql.Tx, id string) (_ artifact.Record, err error) {
	if !idPattern.MatchString(id) {
		return artifact.Record{}, artifact.ErrInvalid
	}
	record := artifact.Record{CapsuleID: id}
	var expected string
	err = tx.QueryRowContext(ctx, `SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?`, s.namespace, id).
		Scan(&record.Capsule, &record.ProducerEnvelope, &expected)
	if errors.Is(err, sql.ErrNoRows) {
		return artifact.Record{}, artifact.ErrNotFound
	}
	if err != nil {
		return artifact.Record{}, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name`, s.namespace, id)
	if err != nil {
		return artifact.Record{}, err
	}
	defer func() { err = errors.Join(err, rows.Close()) }()
	for rows.Next() {
		var a artifact.Artifact
		var content any
		if err := rows.Scan(&a.Name, &a.Binding, &content, &a.ContentSHA256, &a.State); err != nil {
			return artifact.Record{}, err
		}
		// The driver reports a zero-length BLOB as a nil []byte; only SQL NULL
		// means absent content. A zero-length original is present.
		if b, ok := content.([]byte); ok {
			a.Content = append([]byte{}, b...)
		} else if content != nil {
			return artifact.Record{}, fmt.Errorf("%w: content_bytes is not a BLOB", artifact.ErrCorrupt)
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
// inventory and tombstones. It does not remove CLL entries. Erasure requires
// only namespace-scoped existence, not integrity or signer verification.
func (s *Store) Purge(ctx context.Context, id string) (err error) {
	if !idPattern.MatchString(id) {
		return artifact.ErrInvalid
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer finishRollback(tx, &err)
	var exists string
	err = tx.QueryRowContext(ctx, `SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?`, s.namespace, id).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return artifact.ErrNotFound
	}
	if err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `UPDATE capsule_store_artifacts SET content_bytes=NULL, retention_state='purged', purged_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE namespace=? AND capsule_id=? AND retention_state='present'`, s.namespace, id); err != nil {
		return err
	}
	return tx.Commit()
}

func finishRollback(tx *sql.Tx, err *error) {
	if *err != nil {
		if e := tx.Rollback(); e != nil && !errors.Is(e, sql.ErrTxDone) {
			*err = errors.Join(*err, e)
		}
	}
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
	return s.inTx(ctx, func(tx *sql.Tx) error { return s.lookup.Declare(ctx, tx, paths) })
}

// Reindex replaces the namespace's declared paths and rebuilds its index.
func (s *Store) Reindex(ctx context.Context, paths []string) error {
	if _, err := index.NormalizePaths(paths); err != nil {
		return err
	}
	if err := s.Init(ctx); err != nil {
		return err
	}
	return s.inTx(ctx, func(tx *sql.Tx) error { return s.lookup.Reindex(ctx, tx, paths) })
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
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer finishRollback(tx, &err)
	if err = run(tx); err != nil {
		return err
	}
	return tx.Commit()
}
