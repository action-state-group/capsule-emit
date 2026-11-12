package postgres_test

import (
	"crypto/ed25519"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/testutil"
	"github.com/action-state-group/capsule-emit/go/artifact/postgres"
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

// A connection whose default isolation is REPEATABLE READ must not weaken the
// store's own transactions: they open at READ COMMITTED explicitly, so a
// declaration that waited on an in-flight Put still backfills that Put.
func TestPostgresStoreTransactionsIgnoreRepeatableReadDefault(t *testing.T) {
	base := openTestDB(t)
	record, pub := testutil.Record(t, "postgres-rr-default")
	dsn := os.Getenv("CAPSULE_STORAGE_TEST_POSTGRES_DSN")
	sep := "?"
	if strings.Contains(dsn, "?") {
		sep = "&"
	}
	rrDB, err := sql.Open("pgx", dsn+sep+"default_transaction_isolation=repeatable%20read")
	require.NoError(t, err)
	t.Cleanup(func() { assert.NoError(t, rrDB.Close()) })
	var level string
	require.NoError(t, rrDB.QueryRowContext(t.Context(), `SHOW transaction_isolation`).Scan(&level))
	require.Equal(t, "repeatable read", level)
	namespace := fmt.Sprintf("rr-default-%d", time.Now().UnixNano())
	writer, err := postgres.New(rrDB, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, writer.Init(t.Context()))
	// An in-flight PutTx on a READ COMMITTED transaction of the base pool.
	other, err := postgres.New(base, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	tx, err := base.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	require.NoError(t, other.PutTx(t.Context(), tx, record))
	done := make(chan error, 1)
	go func() { done <- writer.InitIndex(t.Context(), []string{"operator"}) }()
	select {
	case err := <-done:
		t.Fatalf("declaration finished while a Put was uncommitted: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	require.NoError(t, tx.Commit())
	require.NoError(t, <-done)
	page, err := writer.Find(t.Context(), artifact.Query{Where: map[string]string{"operator": "test-operator"}})
	require.NoError(t, err)
	assert.Equal(t, []string{record.CapsuleID}, page.IDs)
}
