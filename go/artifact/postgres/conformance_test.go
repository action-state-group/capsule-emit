package postgres_test

import (
	"crypto/ed25519"
	"database/sql"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	"github.com/action-state-group/capsule-emit/go/artifact/postgres"
	"github.com/stretchr/testify/require"
)

// conformanceNamespaces are the namespaces the shared suite writes. Each case
// needs empty storage, so their rows are removed before it runs; other tests
// use their own namespaces and are unaffected.
var conformanceNamespaces = []any{"conformance", "alpha", "beta"}

func TestConformance(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[*sql.DB]{
		NewBacking: func(t *testing.T) *sql.DB {
			db := openTestDB(t)
			store, err := postgres.New(db, "conformance", []ed25519.PublicKey{make([]byte, ed25519.PublicKeySize)})
			require.NoError(t, err)
			require.NoError(t, store.Init(t.Context()))
			for _, table := range []string{"capsule_store_artifacts", "capsule_store_capsules", "capsule_store_index", "capsule_store_index_records", "capsule_store_index_paths"} {
				_, err := db.ExecContext(t.Context(), "DELETE FROM "+table+" WHERE namespace IN ($1,$2,$3)", conformanceNamespaces...)
				require.NoError(t, err)
			}
			return db
		},
		Open: func(_ *testing.T, db *sql.DB, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return postgres.New(db, namespace, trusted)
		},
		Capabilities: artifacttest.Capabilities{Durable: true, SharedBacking: true, DurableIndex: true},
	})
}
