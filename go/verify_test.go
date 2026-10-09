package emit

import (
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/registries"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestKnownRegistriesMatchAuthoritativeSeeds pins the emitter's registry set
// to the agent-action-capsule authoritative registry at the pinned module, so
// values seeded through draft -05 are never reported as unknown.
func TestKnownRegistriesMatchAuthoritativeSeeds(t *testing.T) {
	authoritative, err := registries.LoadAuthoritative()
	require.NoError(t, err)
	assert.Equal(t, authoritative, knownRegistries())
}

func TestIsV4IrreversibilityClass(t *testing.T) {
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityTwoWay))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayRecoverable))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayConsequential))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayTerminal))
	assert.False(t, IsV4IrreversibilityClass(IrreversibilityClass("one_way_consequental")))
}
