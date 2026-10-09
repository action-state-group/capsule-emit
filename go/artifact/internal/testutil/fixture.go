// Package testutil provides synthetic, locally signed artifact test fixtures.
package testutil

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	emit "github.com/action-state-group/capsule-emit-go"
	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

// Record returns a Capsule and exact originals signed by a freshly generated test key.
func Record(t *testing.T, action string) (artifact.Record, ed25519.PublicKey) {
	t.Helper()
	pub, private, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := emit.NewEd25519SigningIdentity(private)
	require.NoError(t, err)
	payload := []byte("{\n  \"question\": \"where next?\", \"n\": 2\n}")
	output := []byte(`{"next_action":"inspect logs"}`)
	sealed, err := emit.Seal(emit.SealInput{
		Capsule: emit.Input{ActionID: action, ActionType: emit.ActionTypeFYI,
			Operator: "test-operator", Developer: "test-developer", Timestamp: time.Date(2026, 9, 8, 1, 2, 3, 0, time.UTC)},
		Payload: json.RawMessage(payload), AgentOutput: json.RawMessage(output), Identity: identity,
	})
	require.NoError(t, err)
	return artifact.Record{CapsuleID: sealed.CapsuleID, Capsule: sealed.Payload, ProducerEnvelope: sealed.Envelope,
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: payload, State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, Content: output, State: artifact.Present},
			{Name: "private_note", Content: []byte("not a producer-authenticated assertion"), State: artifact.Present},
		}}, pub
}
