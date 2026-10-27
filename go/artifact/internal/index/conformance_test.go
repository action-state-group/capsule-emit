package index_test

import (
	"crypto/ed25519"
	"database/sql"
	"net/url"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	"github.com/action-state-group/capsule-emit/go/artifact/memory"
	sqlitestore "github.com/action-state-group/capsule-emit/go/artifact/sqlite"
	"github.com/stretchr/testify/require"
	_ "modernc.org/sqlite"
)

// The backends' own suites cover this package too; running two of them here
// keeps its coverage counted where it is defined.
func TestConformanceThroughBackends(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[struct{}]{
		NewBacking: func(*testing.T) struct{} { return struct{}{} },
		Open: func(_ *testing.T, _ struct{}, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return memory.New(namespace, trusted)
		},
	})
	artifacttest.Run(t, artifacttest.Harness[*sql.DB]{
		NewBacking: func(t *testing.T) *sql.DB {
			uri := (&url.URL{Scheme: "file", Path: filepath.Join(t.TempDir(), "a.db"), RawQuery: "mode=rwc"}).String()
			db, err := sql.Open("sqlite", uri)
			require.NoError(t, err)
			db.SetMaxOpenConns(1)
			_, err = db.Exec("PRAGMA foreign_keys=ON")
			require.NoError(t, err)
			t.Cleanup(func() { require.NoError(t, db.Close()) })
			return db
		},
		Open: func(_ *testing.T, db *sql.DB, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return sqlitestore.New(db, namespace, trusted)
		},
		Capabilities: artifacttest.Capabilities{Durable: true, SharedBacking: true, DurableIndex: true},
	})
}
