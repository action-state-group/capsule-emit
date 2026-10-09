package mysql_test

import (
	"crypto/ed25519"
	"database/sql"
	"fmt"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	mysqlstore "github.com/action-state-group/capsule-emit-go/artifact/mysql"
	"github.com/go-sql-driver/mysql"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Integration tests accept only an explicitly named local disposable database.
// No production config file, CLI profile, or default environment is consulted.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	dsn := os.Getenv("CAPSULE_STORAGE_TEST_DSN")
	if dsn == "" {
		t.Skip("set CAPSULE_STORAGE_TEST_DSN for isolated MySQL integration tests")
	}
	config, err := mysql.ParseDSN(dsn)
	require.NoError(t, err)
	host, _, err := net.SplitHostPort(config.Addr)
	require.NoError(t, err)
	require.Equal(t, "tcp", config.Net)
	require.Equal(t, "127.0.0.1", host)
	require.Equal(t, "capsule_storage_test", config.DBName)
	db, err := sql.Open("mysql", dsn)
	require.NoError(t, err)
	t.Cleanup(func() { assert.NoError(t, db.Close()) })
	require.NoError(t, db.PingContext(t.Context()))
	return db
}

func TestMySQLLifecycle(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "mysql-lifecycle")
	namespace := fmt.Sprintf("test-%d", time.Now().UnixNano())
	store, err := mysqlstore.New(db, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.Put(t.Context(), record))

	// A separate connection pool/SDK handle must recover exact bytes.
	readerDB := openTestDB(t)
	reader, err := mysqlstore.New(readerDB, namespace, []ed25519.PublicKey{pub})
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

	isolated, err := mysqlstore.New(db, namespace+"-other", []ed25519.PublicKey{pub})
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
	_, err = db.ExecContext(t.Context(), `DELETE FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? AND name='private_note'`, namespace, record.CapsuleID)
	require.NoError(t, err)
	_, err = store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrCorrupt)
}

func TestMySQLMissingOriginalAndDigestMismatch(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "missing-original")
	store, err := mysqlstore.New(db, fmt.Sprintf("missing-%d", time.Now().UnixNano()), []ed25519.PublicKey{pub})
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
	_, err := mysqlstore.New(nil, "test", nil)
	require.ErrorIs(t, err, artifact.ErrInvalid)
}
