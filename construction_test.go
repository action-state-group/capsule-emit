package producer

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestCarryAndReceivedBindExactBytes(t *testing.T) {
	artifact := []byte(`{"provider_ack":"PO-9182"}`)
	expected := sha256.Sum256(artifact)
	expectedDigest := hex.EncodeToString(expected[:])

	carried, err := Carry(validInput(), artifact)
	require.NoError(t, err)
	compute := carried.Value["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)
	assert.Equal(t, expectedDigest, compute["carried_input_digest"])
	assert.Equal(t, foreignArtifactType, compute["carried_artifact"].(map[string]any)["type"])

	received, err := Received(validInput(), artifact, "provider-ack")
	require.NoError(t, err)
	reference := received.Value["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)["carried_artifact"].(map[string]any)
	assert.Equal(t, "provider-ack", reference["type"])
	assert.Equal(t, expectedDigest, reference["digest"])
	assert.NotEqual(t, carried.CapsuleID, received.CapsuleID)
}

func TestComposeBindsMembersInCallerOrder(t *testing.T) {
	firstInput := validInput()
	secondInput := validInput()
	secondInput.ActionID = "action-2"
	first, err := Build(firstInput)
	require.NoError(t, err)
	second, err := Build(secondInput)
	require.NoError(t, err)

	composed, err := Compose(validInput(), []BuiltPayload{first, second})
	require.NoError(t, err)
	members := composed.Value["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)["composed_members"].([]any)
	assert.Equal(t, first.CapsuleID, members[0].(map[string]any)["digest"])
	assert.Equal(t, second.CapsuleID, members[1].(map[string]any)["digest"])

	_, err = Compose(validInput(), nil)
	assert.Error(t, err)
	_, err = Compose(validInput(), []BuiltPayload{first, first})
	assert.ErrorContains(t, err, "duplicates")
}

func TestReceivedRejectsAmbiguousArtifact(t *testing.T) {
	_, err := Received(validInput(), nil, "provider-ack")
	assert.Error(t, err)
	_, err = Received(validInput(), []byte("x"), " ")
	assert.Error(t, err)
}
