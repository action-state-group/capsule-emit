package postgres_test

import (
	"database/sql"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// declareDuringOpenPut starts declare while a PutTx is uncommitted, checks that
// the declaration waits for the Put instead of missing its row, then commits
// the Put and checks the record is indexed.
func declareDuringOpenPut(t *testing.T, declare func(artifact.IndexStore) error, prepare func(artifact.IndexStore)) {
	t.Helper()
	db, store, record, _ := regressionStore(t)
	if prepare != nil {
		prepare(store)
	}
	tx, err := db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	require.NoError(t, store.PutTx(t.Context(), tx, record))
	done := make(chan error, 1)
	go func() { done <- declare(store) }()
	select {
	case err := <-done:
		t.Fatalf("declaration finished while a Put was uncommitted: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	require.NoError(t, tx.Commit())
	select {
	case err := <-done:
		require.NoError(t, err)
	case <-time.After(10 * time.Second):
		t.Fatal("declaration did not finish after the Put committed")
	}
	page, err := store.Find(t.Context(), artifact.Query{Where: map[string]string{"operator": "test-operator"}})
	require.NoError(t, err)
	assert.Equal(t, []string{record.CapsuleID}, page.IDs)
}

func TestPostgresInitIndexWaitsForOpenPut(t *testing.T) {
	declareDuringOpenPut(t, func(s artifact.IndexStore) error {
		return s.InitIndex(t.Context(), []string{"operator"})
	}, nil)
}

func TestPostgresReindexWaitsForOpenPut(t *testing.T) {
	declareDuringOpenPut(t, func(s artifact.IndexStore) error {
		return s.Reindex(t.Context(), []string{"operator", "developer"})
	}, func(s artifact.IndexStore) {
		require.NoError(t, s.InitIndex(t.Context(), []string{"operator"}))
	})
}

func TestPostgresPutWaitsForOpenDeclaration(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	// A declaration holds the namespace lock until its transaction ends, so a
	// concurrent Put waits and then sees the declaration.
	require.NoError(t, store.InitIndex(t.Context(), []string{"operator"}))
	done := make(chan error, 1)
	tx, err := db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	_, err = tx.ExecContext(t.Context(), `SELECT pg_advisory_xact_lock(hashtextextended('capsule_store_index:' || $1::text, 0))`, store.Namespace())
	require.NoError(t, err)
	go func() { done <- store.Put(t.Context(), record) }()
	select {
	case err := <-done:
		t.Fatalf("Put finished while the namespace was locked: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	require.NoError(t, tx.Commit())
	require.NoError(t, <-done)
	page, err := store.Find(t.Context(), artifact.Query{Where: map[string]string{"operator": "test-operator"}})
	require.NoError(t, err)
	assert.Equal(t, []string{record.CapsuleID}, page.IDs)
}

func TestPostgresPutTxRequiresReadCommitted(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	tx, err := db.BeginTx(t.Context(), &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	require.ErrorIs(t, store.PutTx(t.Context(), tx, record), artifact.ErrInvalid)
}
