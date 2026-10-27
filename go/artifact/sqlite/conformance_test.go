package sqlite_test

import (
	"crypto/ed25519"
	"database/sql"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	sqlitestore "github.com/action-state-group/capsule-emit/go/artifact/sqlite"
)

func TestConformance(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[*sql.DB]{
		NewBacking: openTestDB,
		Open: func(_ *testing.T, db *sql.DB, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return sqlitestore.New(db, namespace, trusted)
		},
		Capabilities: artifacttest.Capabilities{Durable: true, SharedBacking: true, DurableIndex: true},
	})
}
