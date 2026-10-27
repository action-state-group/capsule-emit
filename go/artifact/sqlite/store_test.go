package sqlite_test

import (
	"crypto/ed25519"
	"database/sql"
	"fmt"
	"net/url"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/testutil"
	sqlitestore "github.com/action-state-group/capsule-emit/go/artifact/sqlite"
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

func TestSQLiteNewRejectsInvalidConfig(t *testing.T) {
	db := openTestDB(t)
	_, pub := testutil.Record(t, "sqlite-new")
	good := []ed25519.PublicKey{pub}
	cases := map[string]struct {
		db        *sql.DB
		namespace string
		trusted   []ed25519.PublicKey
	}{
		"nil db":          {nil, "ns", good},
		"empty namespace": {db, "", good},
		"bad namespace":   {db, "bad namespace!", good},
		"no trusted keys": {db, "ns", nil},
		"bad key size":    {db, "ns", []ed25519.PublicKey{{1, 2, 3}}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := sqlitestore.New(tc.db, tc.namespace, tc.trusted)
			assert.ErrorIs(t, err, artifact.ErrInvalid)
		})
	}
}

func TestSQLiteNamespace(t *testing.T) {
	db := openTestDB(t)
	_, pub := testutil.Record(t, "sqlite-ns")
	store, err := sqlitestore.New(db, "test-namespace", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	assert.Equal(t, "test-namespace", store.Namespace())
}

func TestSQLitePutTxGetTxJoinCallerTransaction(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-tx")
	store, err := sqlitestore.New(db, "test-tx", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))

	// PutTx and GetTx must join a caller-owned transaction: a write and read in
	// the same tx observe each other, and the write persists only on commit.
	tx, err := db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	require.NoError(t, store.PutTx(t.Context(), tx, record))
	got, err := store.GetTx(t.Context(), tx, record.CapsuleID, false)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
	require.NoError(t, tx.Commit())

	got, err = store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
}

func TestSQLiteTxMethodsRejectNilTx(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-niltx")
	store, err := sqlitestore.New(db, "test-niltx", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	assert.ErrorIs(t, store.PutTx(t.Context(), nil, record), artifact.ErrInvalid)
	_, err = store.GetTx(t.Context(), nil, record.CapsuleID, true)
	assert.ErrorIs(t, err, artifact.ErrInvalid)
}

func TestSQLitePurgeRejectsMissingAndMalformed(t *testing.T) {
	db := openTestDB(t)
	_, pub := testutil.Record(t, "sqlite-purge-missing")
	store, err := sqlitestore.New(db, "test-purge-missing", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	assert.ErrorIs(t, store.Purge(t.Context(), fmt.Sprintf("%064x", 0)), artifact.ErrNotFound)
	assert.ErrorIs(t, store.Purge(t.Context(), "not-a-hex-id"), artifact.ErrInvalid)
}

// TestSQLiteReadRejectsNonBlobContent: SQLite's dynamic typing lets a direct
// writer store TEXT in content_bytes; a read fails closed instead of treating it
// as absent or reinterpreting it.
func TestSQLiteReadRejectsNonBlobContent(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-text-content")
	store, err := sqlitestore.New(db, "test-text", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	_, err = db.ExecContext(t.Context(), `UPDATE capsule_store_artifacts SET content_bytes='text' WHERE namespace='test-text' AND name='private_note'`)
	require.NoError(t, err)
	_, err = store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrCorrupt)
}

// TestSQLiteRejectsMalformedIDBeforeIO: a malformed ID is invalid even when the
// database is unusable, because the check runs before any transaction opens.
func TestSQLiteRejectsMalformedIDBeforeIO(t *testing.T) {
	db := openTestDB(t)
	store, err := sqlitestore.New(db, "test-closed", []ed25519.PublicKey{make([]byte, ed25519.PublicKeySize)})
	require.NoError(t, err)
	require.NoError(t, db.Close())
	_, err = store.Get(t.Context(), "not-an-id")
	require.ErrorIs(t, err, artifact.ErrInvalid)
	require.ErrorIs(t, store.Purge(t.Context(), "not-an-id"), artifact.ErrInvalid)
}

// A database provisioned before the index tables existed keeps accepting Put
// and gains the tables, and its records, on InitIndex.
func TestSQLiteIndexOnOlderSchema(t *testing.T) {
	db := openTestDB(t)
	for _, statement := range []string{
		`CREATE TABLE capsule_store_capsules (namespace TEXT NOT NULL, capsule_id TEXT NOT NULL, capsule_bytes BLOB NOT NULL,
		 producer_envelope BLOB NOT NULL, record_sha256 TEXT NOT NULL,
		 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY (namespace, capsule_id))`,
		`CREATE TABLE capsule_store_artifacts (namespace TEXT NOT NULL, capsule_id TEXT NOT NULL, name TEXT NOT NULL,
		 digest_field TEXT NOT NULL, content_bytes BLOB NULL, content_sha256 TEXT NOT NULL, retention_state TEXT NOT NULL,
		 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), purged_at TEXT NULL,
		 PRIMARY KEY (namespace, capsule_id, name))`,
	} {
		_, err := db.Exec(statement)
		require.NoError(t, err)
	}
	record, pub := testutil.Record(t, "sqlite-older-schema")
	store, err := sqlitestore.New(db, "older", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.InitIndex(t.Context(), []string{"operator"}))
	page, err := store.Find(t.Context(), artifact.Query{Where: map[string]string{"operator": "test-operator"}})
	require.NoError(t, err)
	assert.Equal(t, []string{record.CapsuleID}, page.IDs)
}

// Backfill leaves out a row whose Capsule bytes no longer decode, and a
// damaged declaration fails closed.
func TestSQLiteIndexDamagedRows(t *testing.T) {
	db := openTestDB(t)
	record, pub := testutil.Record(t, "sqlite-damaged")
	store, err := sqlitestore.New(db, "damaged", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, store.Put(t.Context(), record))
	_, err = db.Exec(`UPDATE capsule_store_capsules SET capsule_bytes=X'7B' WHERE namespace='damaged'`)
	require.NoError(t, err)
	require.NoError(t, store.InitIndex(t.Context(), []string{"operator"}))
	page, err := store.Find(t.Context(), artifact.Query{})
	require.NoError(t, err)
	assert.Empty(t, page.IDs)

	_, err = db.Exec(`UPDATE capsule_store_index_paths SET paths='{' WHERE namespace='damaged'`)
	require.NoError(t, err)
	_, err = store.Find(t.Context(), artifact.Query{})
	assert.ErrorIs(t, err, artifact.ErrCorrupt)
	require.NoError(t, store.Reindex(t.Context(), []string{"developer"}), "reindex replaces a damaged declaration")
	_, err = store.Find(t.Context(), artifact.Query{Where: map[string]string{"developer": "x"}})
	require.NoError(t, err)
	assert.ErrorIs(t, store.InitIndex(t.Context(), []string{"a", "a"}), artifact.ErrInvalid)
	assert.ErrorIs(t, store.Reindex(t.Context(), []string{"a", "a"}), artifact.ErrInvalid)
}
