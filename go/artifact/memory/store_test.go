package memory_test

import (
	"crypto/ed25519"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	"github.com/action-state-group/capsule-emit/go/artifact/internal/testutil"
	"github.com/action-state-group/capsule-emit/go/artifact/memory"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestConformance(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[struct{}]{
		NewBacking: func(*testing.T) struct{} { return struct{}{} },
		Open: func(_ *testing.T, _ struct{}, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return memory.New(namespace, trusted)
		},
	})
}

// TestReturnedRecordsDoNotAlias: mutating a caller's input or a returned record
// never changes what the store holds.
func TestReturnedRecordsDoNotAlias(t *testing.T) {
	record, pub := testutil.Record(t, "memory-alias")
	store, err := memory.New("test-alias", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Put(t.Context(), record))
	record.Artifacts[0].Content[0] = 'X'
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	got.Capsule[0] = 'X'
	got.Artifacts[0].Content[0] = 'Y'
	again, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, byte('{'), again.Capsule[0])
	assert.Equal(t, byte('{'), again.Artifacts[0].Content[0])
}
