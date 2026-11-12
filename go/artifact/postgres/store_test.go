package postgres_test

import (
	"crypto/ed25519"
	"database/sql"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/testutil"
	"github.com/action-state-group/capsule-emit/go/artifact/postgres"
	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Integration tests accept only an explicitly named local disposable database.
// No production config file, CLI profile, or default environment is consulted.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	dsn := os.Getenv("CAPSULE_STORAGE_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("set CAPSULE_STORAGE_TEST_POSTGRES_DSN for isolated PostgreSQL integration tests")
	}
	config, err := pgconn.ParseConfig(dsn)
	require.NoError(t, err)
	require.Equal(t, "127.0.0.1", config.Host)
	require.Equal(t, "capsule_storage_test", config.Database)
	db, err := sql.Open("pgx", dsn)
	require.NoError(t, err)
	t.Cleanup(func() { assert.NoError(t, db.Close()) })
	require.NoError(t, db.PingContext(t.Context()))
	return db
}

func TestPostgresLifecycle(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "postgres-lifecycle")
	namespace := fmt.Sprintf("test-%d", time.Now().UnixNano())
	store, err := postgres.New(db, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.Put(t.Context(), record))

	// A separate connection pool/SDK handle must recover exact bytes.
	readerDB := openTestDB(t)
	reader, err := postgres.New(readerDB, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	got, err := reader.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
	assert.Equal(t, record.ProducerEnvelope, got.ProducerEnvelope)
	byName := map[string]artifact.Artifact{}
	for _, a := range got.Artifacts {
		byName[a.Name] = a
	}
	assert.Equal(t, record.Artifacts[0].Content, byName["payload"].Content)

	conflict := record
	conflict.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
	conflict.Artifacts[2].Content = []byte("changed unbound attachment")
	require.ErrorIs(t, store.Put(t.Context(), conflict), artifact.ErrConflict)

	isolated, err := postgres.New(db, namespace+"-other", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	_, err = isolated.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrNotFound)
	tx, err := db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	require.NoError(t, isolated.PutTx(t.Context(), tx, record))
	require.NoError(t, tx.Rollback())
	_, err = isolated.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrNotFound)

	// Retry concurrency is serialized by the immutable Capsule primary key.
	var wg sync.WaitGroup
	errs := make(chan error, 4)
	for range 4 {
		wg.Go(func() { errs <- store.Put(t.Context(), record) })
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		require.NoError(t, err)
	}

	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	got, err = reader.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	for _, a := range got.Artifacts {
		assert.Equal(t, artifact.Purged, a.State)
		assert.Nil(t, a.Content)
		assert.Len(t, a.ContentSHA256, 64)
	}
	require.ErrorIs(t, store.Put(t.Context(), record), artifact.ErrPurged)

	// Inventory corruption must fail closed, even for unbound originals.
	_, err = db.ExecContext(t.Context(), `DELETE FROM capsule_store_artifacts WHERE namespace=$1 AND capsule_id=$2 AND name='private_note'`, namespace, record.CapsuleID)
	require.NoError(t, err)
	_, err = store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrCorrupt)
}

func TestPostgresMissingOriginalAndDigestMismatch(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "missing-original")
	store, err := postgres.New(db, fmt.Sprintf("missing-%d", time.Now().UnixNano()), []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	original := record.Artifacts[0].Content
	record.Artifacts[0].Content = []byte(`{"bad":true}`)
	require.ErrorIs(t, store.Put(t.Context(), record), artifact.ErrDigestMismatch)
	_, err = store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrNotFound)
	record.Artifacts[0].Content = original
	record.Artifacts[1].State = artifact.NeverRetained
	record.Artifacts[1].Content = nil
	require.NoError(t, store.Put(t.Context(), record))
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	checks, err := artifact.Verify(got, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	assert.Equal(t, artifact.NeverRetained, checks["agent_output"].State)
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	got, err = store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	checks, err = artifact.Verify(got, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	assert.Equal(t, artifact.NeverRetained, checks["agent_output"].State)
}

func TestConstructorRejectsMissingConfiguration(t *testing.T) {
	_, err := postgres.New(nil, "test", nil)
	require.ErrorIs(t, err, artifact.ErrInvalid)
}
