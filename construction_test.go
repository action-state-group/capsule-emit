package emit

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
	assert.NotContains(t, reference, "slot")
	assert.NotEqual(t, carried.CapsuleID, received.CapsuleID)
}

func TestBuildCompositionUsesCanonicalSlotOrder(t *testing.T) {
	who := buildMember(t, "who")
	can := buildMember(t, "can")
	did := buildMember(t, "did")
	audit := buildMember(t, "audit")

	composed, err := BuildComposition(validInput(), Audit(audit), Did(did), Who(who), Can(can))
	require.NoError(t, err)
	members := composed.Value["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)["composed_members"].([]any)
	require.Len(t, members, 4)
	for index, expected := range []struct {
		slot string
		id   string
	}{
		{slot: "who", id: who.CapsuleID},
		{slot: "can", id: can.CapsuleID},
		{slot: "did", id: did.CapsuleID},
		{slot: "audit", id: audit.CapsuleID},
	} {
		reference := members[index].(map[string]any)
		assert.Equal(t, expected.slot, reference["slot"])
		assert.Equal(t, expected.id, reference["digest"])
		assert.Equal(t, "capsule", reference["type"])
		assert.Equal(t, "SHA-256", reference["digest_alg"])
	}

	reordered, err := BuildComposition(validInput(), Who(who), Can(can), Did(did), Audit(audit))
	require.NoError(t, err)
	assert.Equal(t, composed.CapsuleID, reordered.CapsuleID)
	assert.Equal(t, composed.JSON, reordered.JSON)
}

func TestBuildCompositionRejectsInvalidMembership(t *testing.T) {
	member := buildMember(t, "member")
	other := buildMember(t, "other")

	_, err := BuildComposition(validInput())
	assert.ErrorContains(t, err, "at least one slot member")
	_, err = BuildComposition(validInput(), SlotMember{})
	assert.ErrorContains(t, err, "invalid slot")
	_, err = BuildComposition(validInput(), Who(member), Who(other))
	assert.ErrorContains(t, err, "duplicates who slot")
	_, err = BuildComposition(validInput(), Who(member), Can(member))
	assert.ErrorContains(t, err, "duplicates Capsule ID")

	malformed := member
	malformed.CapsuleID = "bad"
	_, err = BuildComposition(validInput(), Did(malformed))
	assert.ErrorContains(t, err, "malformed Capsule ID")

	mismatched := member
	mismatched.CapsuleID = other.CapsuleID
	_, err = BuildComposition(validInput(), Audit(mismatched))
	assert.ErrorContains(t, err, "not a matching verified format-4 Capsule")
}

func TestReceivedRejectsAmbiguousArtifact(t *testing.T) {
	_, err := Received(validInput(), nil, "provider-ack")
	assert.Error(t, err)
	_, err = Received(validInput(), []byte("x"), " ")
	assert.Error(t, err)
}

func buildMember(t *testing.T, actionID string) BuiltPayload {
	t.Helper()
	input := validInput()
	input.ActionID = actionID
	member, err := Build(input)
	require.NoError(t, err)
	return member
}
