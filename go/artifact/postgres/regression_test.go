package postgres_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"database/sql"
	"fmt"
	"testing"
	"time"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/testutil"
	"github.com/action-state-group/capsule-emit/go/artifact/postgres"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Each regression owns a namespace in the explicitly configured disposable DB.
func regressionStore(t *testing.T) (*sql.DB, *postgres.Store, artifact.Record, ed25519.PublicKey) {
	t.Helper()
	db := openTestDB(t)
	record, pub := testutil.Record(t, t.Name())
	store, err := postgres.New(db, fmt.Sprintf("regression-%d", time.Now().UnixNano()), []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	return db, store, record, pub
}

func TestPostgresPurgeDamagedRecords(t *testing.T) {
	for _, kind := range []string{"missing_inventory", "capsule_bytes", "unbound_bytes", "untrusted_signer"} {
		t.Run(kind, func(t *testing.T) {
			db, store, record, _ := regressionStore(t)
			require.NoError(t, store.Put(t.Context(), record))
			want := artifact.ErrCorrupt
			survivors := len(record.Artifacts)
			switch kind {
			case "missing_inventory":
				_, err := db.ExecContext(t.Context(), `DELETE FROM capsule_store_artifacts WHERE namespace=$1 AND capsule_id=$2 AND name='payload'`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
				survivors--
			case "capsule_bytes":
				_, err := db.ExecContext(t.Context(), `UPDATE capsule_store_capsules SET capsule_bytes='{}' WHERE namespace=$1 AND capsule_id=$2`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
			case "unbound_bytes":
				_, err := db.ExecContext(t.Context(), `UPDATE capsule_store_artifacts SET content_bytes='altered' WHERE namespace=$1 AND capsule_id=$2 AND name='private_note'`, store.Namespace(), record.CapsuleID)
				require.NoError(t, err)
			case "untrusted_signer":
				pub, _, err := ed25519.GenerateKey(rand.Reader)
				require.NoError(t, err)
				store, err = postgres.New(db, store.Namespace(), []ed25519.PublicKey{pub})
				require.NoError(t, err)
				want = artifact.ErrUntrustedSigner
			}
			_, err := store.Get(t.Context(), record.CapsuleID)
			require.ErrorIs(t, err, want)
			require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
			require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
			var present, tombstones int
			require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM capsule_store_artifacts WHERE namespace=$1 AND capsule_id=$2 AND content_bytes IS NOT NULL`, store.Namespace(), record.CapsuleID).Scan(&present))
			require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM capsule_store_artifacts WHERE namespace=$1 AND capsule_id=$2 AND retention_state='purged' AND purged_at IS NOT NULL`, store.Namespace(), record.CapsuleID).Scan(&tombstones))
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

func TestPostgresPurgeNamespaceAndInput(t *testing.T) {
	db, store, record, pub := regressionStore(t)
	require.NoError(t, store.Put(t.Context(), record))
	other, err := postgres.New(db, store.Namespace()+"-other", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.ErrorIs(t, other.Purge(t.Context(), record.CapsuleID), artifact.ErrNotFound)
	require.ErrorIs(t, store.Purge(t.Context(), "bad"), artifact.ErrInvalid)
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	for _, a := range got.Artifacts {
		assert.Equal(t, artifact.Present, a.State)
	}
}

func TestPostgresCallerTransactionRollback(t *testing.T) {
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
	_, err = postgres.New(db, store.Namespace(), []ed25519.PublicKey{{1}})
	require.ErrorIs(t, err, artifact.ErrInvalid)
}

func TestPostgresGetTxLockSerializesPurge(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	require.NoError(t, store.Put(t.Context(), record))
	tx, err := db.BeginTx(t.Context(), nil) // READ COMMITTED, the PostgreSQL default
	require.NoError(t, err)
	before, err := store.GetTx(t.Context(), tx, record.CapsuleID, false)
	require.NoError(t, err)
	assert.Equal(t, artifact.Present, before.Artifacts[0].State)
	locked, err := store.GetTx(t.Context(), tx, record.CapsuleID, true)
	require.NoError(t, err)
	assert.Equal(t, artifact.Present, locked.Artifacts[0].State)
	// The locked parent must block a separate purge until the caller releases it.
	ctx, cancel := context.WithTimeout(t.Context(), 250*time.Millisecond)
	defer cancel()
	require.ErrorIs(t, store.Purge(ctx, record.CapsuleID), context.DeadlineExceeded)
	require.NoError(t, tx.Rollback())
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	// Once released, a locking read in a new transaction sees the erasure.
	tx, err = db.BeginTx(t.Context(), nil)
	require.NoError(t, err)
	current, err := store.GetTx(t.Context(), tx, record.CapsuleID, true)
	require.NoError(t, err)
	for _, a := range current.Artifacts {
		assert.Equal(t, artifact.Purged, a.State)
	}
	require.NoError(t, tx.Rollback())
}

// A REPEATABLE READ caller whose snapshot predates an erasure cannot lock the
// changed row: PostgreSQL reports a serialization failure instead of returning
// a stale read, and the caller replays its whole transaction.
func TestPostgresGetTxLockRepeatableReadFailsClosed(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	require.NoError(t, store.Put(t.Context(), record))
	tx, err := db.BeginTx(t.Context(), &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	require.NoError(t, err)
	defer func() { _ = tx.Rollback() }()
	_, err = store.GetTx(t.Context(), tx, record.CapsuleID, false)
	require.NoError(t, err)
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	_, err = store.GetTx(t.Context(), tx, record.CapsuleID, true)
	var pgErr *pgconn.PgError
	require.ErrorAs(t, err, &pgErr)
	assert.Equal(t, "40001", pgErr.Code)
}

// An application row and the artifact commit or roll back together, which is
// how a caller composes the store with its own checkpointed-log identity row.
func TestPostgresCallerRowAndRecordAreAtomic(t *testing.T) {
	db, store, record, _ := regressionStore(t)
	table := fmt.Sprintf("caller_log_%d", time.Now().UnixNano())
	_, err := db.ExecContext(t.Context(), `CREATE TABLE `+table+` (capsule_id text PRIMARY KEY)`)
	require.NoError(t, err)
	t.Cleanup(func() { _, _ = db.Exec(`DROP TABLE ` + table) })
	for _, commit := range []bool{false, true} {
		tx, err := db.BeginTx(t.Context(), nil)
		require.NoError(t, err)
		require.NoError(t, store.PutTx(t.Context(), tx, record))
		_, err = tx.ExecContext(t.Context(), `INSERT INTO `+table+` VALUES ($1)`, record.CapsuleID)
		require.NoError(t, err)
		if commit {
			require.NoError(t, tx.Commit())
		} else {
			require.NoError(t, tx.Rollback())
		}
		var rows int
		require.NoError(t, db.QueryRowContext(t.Context(), `SELECT COUNT(*) FROM `+table).Scan(&rows))
		_, getErr := store.Get(t.Context(), record.CapsuleID)
		if commit {
			assert.Equal(t, 1, rows)
			require.NoError(t, getErr)
		} else {
			assert.Zero(t, rows)
			require.ErrorIs(t, getErr, artifact.ErrNotFound)
		}
	}
}
