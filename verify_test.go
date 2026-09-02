package emit

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestKnownRegistriesMatchV4Seeds(t *testing.T) {
	set := func(values ...string) map[string]bool {
		result := make(map[string]bool, len(values))
		for _, value := range values {
			result[value] = true
		}
		return result
	}
	assert.Equal(t, map[string]map[string]bool{
		"verdict_class": set(
			"executed", "blocked", "hitl_dispatched", "denied", "timeout", "errored",
			"engine_failure", "deferred", "needs_decision", "expired", "escalated",
			"resolved", "epoch_boundary",
		),
		"disposition.decision": set("accept", "reject", "needs_input", "deferred"),
		"effect.type":          set("write_order", "send_payment"),
		"irreversibility_class": set(
			"two_way", "one_way_recoverable", "one_way_consequential", "one_way_terminal",
		),
		"effect_attestation": set("gate_executed", "runtime_claimed"),
		"chain.relation":     set("confirms", "supersedes", "epoch_opens"),
	}, knownRegistries())
}

func TestIsV4IrreversibilityClass(t *testing.T) {
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityTwoWay))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayRecoverable))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayConsequential))
	assert.True(t, IsV4IrreversibilityClass(IrreversibilityOneWayTerminal))
	assert.False(t, IsV4IrreversibilityClass(IrreversibilityClass("one_way_consequental")))
}
