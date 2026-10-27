package index

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync/atomic"

	"github.com/action-state-group/capsule-emit/go/artifact"
)

// Dialect holds the SQL that differs between the SQLite and MySQL backends.
type Dialect struct {
	// TablesExist counts the index tables; it must return 3 once they exist.
	TablesExist string
	// LockShared and LockUpdate end a SELECT that must see, and hold, the
	// latest committed rows (MySQL FOR SHARE / FOR UPDATE; empty for SQLite,
	// which serializes writers).
	LockShared, LockUpdate string
	// InsertIgnore starts an insert that skips rows already present.
	InsertIgnore string
}

// SQL is one SQL backend's view of the index tables.
type SQL struct {
	Dialect
	Namespace string
	exist     atomic.Bool
}

// declared reads the namespace's declaration inside tx. Databases provisioned
// before the index tables existed have no declaration until init runs again.
func (s *SQL) declared(ctx context.Context, tx *sql.Tx, lock string) ([]string, bool, error) {
	if !s.exist.Load() {
		var n int
		if err := tx.QueryRowContext(ctx, s.TablesExist).Scan(&n); err != nil {
			return nil, false, err
		}
		if n < 3 {
			return nil, false, nil
		}
		s.exist.Store(true)
	}
	var stored string
	err := tx.QueryRowContext(ctx, `SELECT paths FROM capsule_store_index_paths WHERE namespace=?`+lock, s.Namespace).Scan(&stored)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	paths, err := DecodePaths(stored)
	return paths, err == nil, err
}

// Admit indexes a record that PutTx has just inserted, when the namespace
// declares paths. Callers run it in the same transaction as the insert.
func (s *SQL) Admit(ctx context.Context, tx *sql.Tx, id string, capsule []byte) error {
	paths, ok, err := s.declared(ctx, tx, s.LockShared)
	if err != nil || !ok {
		return err
	}
	return s.add(ctx, tx, paths, id, capsule)
}

func (s *SQL) add(ctx context.Context, tx *sql.Tx, paths []string, id string, capsule []byte) error {
	extracted, err := Extract(capsule, paths)
	if err != nil {
		return err
	}
	var ts any
	if extracted.HasTS {
		ts = extracted.TS
	}
	result, err := tx.ExecContext(ctx, s.InsertIgnore+` capsule_store_index_records (namespace,capsule_id,ts) VALUES(?,?,?)`, s.Namespace, id, ts)
	if err != nil {
		return err
	}
	if n, err := result.RowsAffected(); err != nil || n == 0 {
		return err // already indexed
	}
	seq, err := result.LastInsertId()
	if err != nil {
		return err
	}
	for _, e := range extracted.Entries {
		if _, err := tx.ExecContext(ctx, `INSERT INTO capsule_store_index (namespace,path,value_sha256,seq) VALUES(?,?,?,?)`,
			s.Namespace, e.Path, ValueKey(e.Value), seq); err != nil {
			return err
		}
	}
	return nil
}

// Declare runs InitIndex's declaration inside tx, after the caller provisioned
// the tables. A concurrent PutTx inserts its Capsule row before Admit reads the
// declaration, so no record escapes: if the row exists before the backfill's
// locking scan reaches it, the scan waits for its commit and indexes it;
// otherwise Admit's locking read waits on the uncommitted declaration row and
// then indexes the record itself. This needs no gap locks, so it holds under
// READ COMMITTED too.
func (s *SQL) Declare(ctx context.Context, tx *sql.Tx, paths []string) error {
	normalized, err := NormalizePaths(paths)
	if err != nil {
		return err
	}
	current, ok, err := s.declared(ctx, tx, s.LockUpdate)
	if err != nil {
		return err
	}
	if ok {
		if !slices.Equal(current, normalized) {
			return fmt.Errorf("%w: namespace already indexes %s", artifact.ErrConflict, EncodePaths(current))
		}
		return nil
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)`, s.Namespace, EncodePaths(normalized)); err != nil {
		return err
	}
	return s.backfill(ctx, tx, normalized)
}

// Reindex replaces the declaration and rebuilds the index inside tx.
func (s *SQL) Reindex(ctx context.Context, tx *sql.Tx, paths []string) error {
	normalized, err := NormalizePaths(paths)
	if err != nil {
		return err
	}
	// Lock the declaration first, so a concurrent Put waits for the rebuild.
	if _, _, err := s.declared(ctx, tx, s.LockUpdate); err != nil && !errors.Is(err, artifact.ErrCorrupt) {
		return err
	}
	for _, statement := range []string{
		`DELETE FROM capsule_store_index WHERE namespace=?`,
		`DELETE FROM capsule_store_index_records WHERE namespace=?`,
		`DELETE FROM capsule_store_index_paths WHERE namespace=?`,
	} {
		if _, err := tx.ExecContext(ctx, statement, s.Namespace); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)`, s.Namespace, EncodePaths(normalized)); err != nil {
		return err
	}
	return s.backfill(ctx, tx, normalized)
}

// backfill indexes the stored records in creation order. A record whose
// Capsule bytes no longer decode is left out; Get reports it as corrupt.
func (s *SQL) backfill(ctx context.Context, tx *sql.Tx, paths []string) (err error) {
	type stored struct {
		id      string
		capsule []byte
	}
	rows, err := tx.QueryContext(ctx, `SELECT capsule_id,capsule_bytes FROM capsule_store_capsules WHERE namespace=? ORDER BY created_at,capsule_id`+s.LockShared, s.Namespace)
	if err != nil {
		return err
	}
	var all []stored
	for rows.Next() {
		var r stored
		if err := rows.Scan(&r.id, &r.capsule); err != nil {
			return errors.Join(err, rows.Close())
		}
		all = append(all, r)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	for _, r := range all {
		if err := s.add(ctx, tx, paths, r.id, r.capsule); err != nil && !errors.Is(err, artifact.ErrCorrupt) {
			return err
		}
	}
	return nil
}

// Find answers q inside tx.
func (s *SQL) Find(ctx context.Context, tx *sql.Tx, q artifact.Query) (_ artifact.Page, err error) {
	paths, ok, err := s.declared(ctx, tx, "")
	if err != nil {
		return artifact.Page{}, err
	}
	if !ok {
		return artifact.Page{}, fmt.Errorf("%w: no index paths declared", artifact.ErrInvalid)
	}
	c, err := Check(q, paths)
	if err != nil {
		return artifact.Page{}, err
	}
	var query strings.Builder
	var args []any
	if len(c.Where) == 0 {
		query.WriteString(`SELECT r.seq,r.capsule_id FROM capsule_store_index_records r WHERE r.namespace=? AND r.seq>?`)
		args = append(args, s.Namespace, c.After)
	} else {
		query.WriteString(`SELECT r.seq,r.capsule_id FROM capsule_store_index i0 JOIN capsule_store_index_records r ON r.seq=i0.seq` +
			` WHERE i0.namespace=? AND i0.path=? AND i0.value_sha256=? AND i0.seq>?`)
		args = append(args, s.Namespace, c.Where[0].Path, ValueKey(c.Where[0].Value), c.After)
		for _, w := range c.Where[1:] {
			query.WriteString(` AND EXISTS (SELECT 1 FROM capsule_store_index i WHERE i.namespace=r.namespace AND i.path=? AND i.value_sha256=? AND i.seq=r.seq)`)
			args = append(args, w.Path, ValueKey(w.Value))
		}
	}
	if c.HasSince {
		query.WriteString(` AND r.ts>=?`)
		args = append(args, c.Since)
	}
	query.WriteString(` ORDER BY r.seq LIMIT ?`)
	args = append(args, c.Limit+1)
	rows, err := tx.QueryContext(ctx, query.String(), args...)
	if err != nil {
		return artifact.Page{}, err
	}
	defer func() { err = errors.Join(err, rows.Close()) }()
	page := artifact.Page{IDs: []string{}}
	var last int64
	for rows.Next() {
		var seq int64
		var id string
		if err := rows.Scan(&seq, &id); err != nil {
			return artifact.Page{}, err
		}
		if len(page.IDs) == c.Limit {
			page.Next = Cursor(last)
			break
		}
		page.IDs = append(page.IDs, id)
		last = seq
	}
	if err := rows.Err(); err != nil {
		return artifact.Page{}, err
	}
	return page, nil
}
