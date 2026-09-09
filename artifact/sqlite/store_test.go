package sqlite_test

import (
	"crypto/ed25519"
	"database/sql"
	"fmt"
	"net/url"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	sqlitestore "github.com/action-state-group/capsule-emit-go/artifact/sqlite"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	_ "modernc.org/sqlite"
)

// openTestDB opens a disposable file-backed SQLite database. Unlike the MySQL
// backend, SQLite needs no external server, so these tests always run. A single
// open connection and foreign_keys ON match the CLI's runtime configuration.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "artifacts.db")
	uri := (&url.URL{Scheme: "file", Path: path, RawQuery: "mode=rwc"}).String()
	db, err := sql.Open("sqlite", uri)
	require.NoError(t, err)
	db.SetMaxOpenConns(1)
	for _, pragma := range []string{"PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000", "PRAGMA journal_mode=WAL"} {
		_, err := db.Exec(pragma)
		require.NoError(t, err)
	}
	t.Cleanup(func() { assert.NoError(t, db.Close()) })
	return db
}

func TestSQLiteLifecycle(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-lifecycle")
	namespace := "test-lifecycle"
	store, err := sqlitestore.New(db, namespace, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	// Init and Put are both idempotent.
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.Put(t.Context(), record))

	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
	assert.Equal(t, record.ProducerEnvelope, got.ProducerEnvelope)
	byName := map[string]artifact.Artifact{}
	for _, a := range got.Artifacts {
		byName[a.Name] = a
	}
	assert.Equal(t, record.Artifacts[0].Content, byName["payload"].Content)
	assert.Equal(t, record.Artifacts[1].Content, byName["agent_output"].Content)
}

func TestSQLiteConflictOnChangedBytes(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-conflict")
	store, err := sqlitestore.New(db, "test-conflict", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))

	conflict := record
	conflict.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
	conflict.Artifacts[2].Content = []byte("changed unbound attachment")
	err = store.Put(t.Context(), conflict)
	assert.ErrorIs(t, err, artifact.ErrConflict)
}

func TestSQLiteGetNotFound(t *testing.T) {
	db := openTestDB(t)
	_, pub := testutil.Record(t, "sqlite-missing")
	store, err := sqlitestore.New(db, "test-missing", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	_, err = store.Get(t.Context(), fmt.Sprintf("%064x", 0))
	assert.ErrorIs(t, err, artifact.ErrNotFound)
}

func TestSQLitePurgeMarksOriginalsAndBlocksResurrection(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-purge")
	store, err := sqlitestore.New(db, "test-purge", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	// Purge is idempotent and preserves the commitment/tombstone.
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))

	// Get still succeeds; purged originals are reported unavailable, not erased
	// from the inventory.
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	for _, a := range got.Artifacts {
		if a.Binding != "" {
			assert.Equal(t, artifact.Purged, a.State)
		}
	}
	// A retry after purge cannot resurrect originals.
	require.ErrorIs(t, store.Put(t.Context(), record), artifact.ErrPurged)
}

func TestSQLiteReadRejectsMalformedID(t *testing.T) {
	db := openTestDB(t)
	_, pub := testutil.Record(t, "sqlite-badid")
	store, err := sqlitestore.New(db, "test-badid", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	_, err = store.Get(t.Context(), "not-a-hex-id")
	assert.ErrorIs(t, err, artifact.ErrInvalid)
}
