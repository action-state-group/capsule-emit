package mysql_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"database/sql"
	"fmt"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	mysqlstore "github.com/action-state-group/capsule-emit-go/artifact/mysql"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Each regression owns a namespace in the explicitly configured disposable DB.
func regressionStore(t *testing.T) (*sql.DB, *mysqlstore.Store, artifact.Record, ed25519.PublicKey) {
	t.Helper()
	db := openTestDB(t)
	record, pub := testutil.Record(t, t.Name())
	store, err := mysqlstore.New(db, fmt.Sprintf("regression-%d", time.Now().UnixNano()), []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	return db, store, record, pub
}

func TestMySQLPurgeDamagedRecords(t *testing.T) {
	for _, kind := range []string{"missing_inventory", "capsule_bytes", "unbound_bytes", "untrusted_signer"} {
		t.Run(kind, func(t *testing.T) {
			db, store, record, _ := regressionStore(t)
			require.NoError(t, store.Put(t.Context(), record))
			want := artifact.ErrCorrupt
			survivors := len(record.Artifacts)
			switch kind {
			case "missing_inventory":
				_, err := db.ExecContext(t.Context(), `DELETE FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? AND name='payload'`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
				survivors--
			case "capsule_bytes":
				_, err := db.ExecContext(t.Context(), `UPDATE capsule_store_capsules SET capsule_bytes='{}' WHERE namespace=? AND capsule_id=?`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
			case "unbound_bytes":
				_, err := db.ExecContext(t.Context(), `UPDATE capsule_store_artifacts SET content_bytes='altered' WHERE namespace=? AND capsule_id=? AND name='private_note'`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
			case "untrusted_signer":
				pub, _, err := ed25519.GenerateKey(rand.Reader)
				require.NoError(t, err)
				store, err = mysqlstore.New(db, store.Namespace(), []ed25519.PublicKey{pub})
				require.NoError(t, err)
				want = artifact.ErrUntrustedSigner
			}
			_, err := store.Get(t.Context(), record.CapsuleID)
			require.ErrorIs(t, err, want)
			require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
			require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
			var present, tombstones int
			require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? AND content_bytes IS NOT NULL`, store.Namespace(), record.CapsuleID).Scan(&present))
			require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? AND retention_state='purged' AND purged_at IS NOT NULL`, store.Namespace(), record.CapsuleID).Scan(&tombstones))
			assert.Zero(t, present)
			assert.Equal(t, survivors, tombstones)
			_, err = store.Get(t.Context(), record.CapsuleID)
			if kind == "unbound_bytes" {
				// Erasure removes the damaged bytes; the unchanged inventory still verifies.
				require.NoError(t, err)
			} else {
				require.ErrorIs(t, err, want)
			}
		})
	}
}

func TestMySQLPurgeNamespaceAndInput(t *testing.T) {
	db, store, record, pub := regressionStore(t)
	require.NoError(t, store.Put(t.Context(), record))
	other, err := mysqlstore.New(db, store.Namespace()+"-other", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.ErrorIs(t, other.Purge(t.Context(), record.CapsuleID), artifact.ErrNotFound)
	require.ErrorIs(t, store.Purge(t.Context(), "bad"), artifact.ErrInvalid)
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	for _, a := range got.Artifacts {
		assert.Equal(t, artifact.Present, a.State)
	}
}

func TestMySQLCallerTransactionRollback(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	tx, err := db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	require.NoError(t, store.PutTx(t.Context(), tx, record))
	for _, lock := range []bool{false, true} {
		got, err := store.GetTx(t.Context(), tx, record.CapsuleID, lock)
		require.NoError(t, err)
		assert.Equal(t, record.Capsule, got.Capsule)
		assert.Equal(t, record.ProducerEnvelope, got.ProducerEnvelope)
	}
	require.NoError(t, tx.Rollback())
	_, err = store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrNotFound)
	require.ErrorIs(t, store.PutTx(t.Context(), nil, record), artifact.ErrInvalid)
	_, err = store.GetTx(t.Context(), nil, record.CapsuleID, true)
	require.ErrorIs(t, err, artifact.ErrInvalid)
	_, err = mysqlstore.New(db, store.Namespace(), []ed25519.PublicKey{{1}})
	require.ErrorIs(t, err, artifact.ErrInvalid)
}

func TestMySQLGetTxCurrentReadAndSerialization(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	require.NoError(t, store.Put(t.Context(), record))
	tx, err := db.BeginTx(t.Context(), &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	require.NoError(t, err)
	// Establish a snapshot before erasure in another connection.
	before, err := store.GetTx(t.Context(), tx, record.CapsuleID, false)
	require.NoError(t, err)
	assert.Equal(t, artifact.Present, before.Artifacts[0].State)
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	current, err := store.GetTx(t.Context(), tx, record.CapsuleID, true)
	require.NoError(t, err)
	for _, a := range current.Artifacts {
		assert.Equal(t, artifact.Purged, a.State)
	}
	// The locked parent must block a separate purge until the caller releases it.
	ctx, cancel := context.WithTimeout(t.Context(), 250*time.Millisecond)
	defer cancel()
	require.ErrorIs(t, store.Purge(ctx, record.CapsuleID), context.DeadlineExceeded)
	require.NoError(t, tx.Rollback())
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
}

func TestMySQLSDKWritesUTC(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	// One pooled connection makes the non-UTC session setting apply to every write.
	db.SetMaxOpenConns(1)
	_, err := db.ExecContext(t.Context(), `SET time_zone = '+09:00'`)
	require.NoError(t, err)
	var zone string
	require.NoError(t, db.QueryRowContext(t.Context(), `SELECT @@session.time_zone`).Scan(&zone))
	require.Equal(t, "+09:00", zone)
	require.NoError(t, store.Put(t.Context(), record))
	rows, err := db.QueryContext(t.Context(), `SELECT ABS(TIMESTAMPDIFF(SECOND,created_at,UTC_TIMESTAMP(6))) FROM capsule_store_capsules WHERE namespace=? AND capsule_id=? UNION ALL SELECT ABS(TIMESTAMPDIFF(SECOND,created_at,UTC_TIMESTAMP(6))) FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=?`, store.Namespace(), record.CapsuleID, store.Namespace(), record.CapsuleID)
	require.NoError(t, err)
	count := 0
	for rows.Next() {
		var seconds int
		require.NoError(t, rows.Scan(&seconds))
		assert.Less(t, seconds, 30)
		count++
	}
	require.NoError(t, rows.Err())
	require.NoError(t, rows.Close())
	assert.Equal(t, 1+len(record.Artifacts), count)
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	var coherent int
	require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? AND purged_at >= created_at AND TIMESTAMPDIFF(SECOND,created_at,purged_at) < 30`, store.Namespace(), record.CapsuleID).Scan(&coherent))
	assert.Equal(t, len(record.Artifacts), coherent)
}
