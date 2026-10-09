package emit

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const (
	capsuleEmitFixturePath         = "testdata/capsule-emit/producer-envelope-valid-capsule.json"
	capsuleEmitFixtureSHA          = "cddbb90695c73ec12265089df554defba2a1f6e391492d02eaab15c5850cb51b"
	capsuleEmitCapsuleID           = "8c0ee756765b62206b222884cde80ded483a4ad34e6b38c9a73c3a4d37d8d45e"
	capsuleEmitSlotsFixturePath    = "testdata/capsule-emit/slot-composition-valid-capsule.json"
	capsuleEmitSlotsFixtureSHA     = "db5cf337051352478a8a834aa91d248f114a3167fcf2f8349ca016e618fb2bed"
	capsuleEmitSlotsCompositionID  = "027d99036fe4f8c98a2c4903c0ccb809211cb517fe3cb5a6b7292d0e825f4158"
	capsuleEmitSlotsCarryCapsuleID = "646f0bd87fa9e7dd1d7441f282255ce65ca8732d41652d6d75077b647cdf8785"
)

func TestCapsuleEmitStoredCapsuleInteroperability(t *testing.T) {
	fixture, err := os.ReadFile(capsuleEmitFixturePath)
	require.NoError(t, err)
	digest := sha256.Sum256(fixture)
	require.Equal(t, capsuleEmitFixtureSHA, hex.EncodeToString(digest[:]))

	// Keeping local-only fields present makes this the regression sensor: the
	// pre-fix canonicalization incorrectly included them in the ID preimage.
	verified, err := VerifyCapsule(fixture)
	require.NoError(t, err)
	assert.True(t, verified.OK)
	require.NotNil(t, verified.CapsuleID)
	assert.Equal(t, capsuleEmitCapsuleID, *verified.CapsuleID)

	stored := decodeFixture(t, fixture)
	envelopeHex, ok := stored["signature"].(string)
	require.True(t, ok)
	keyID, ok := stored["key_id"].(string)
	require.True(t, ok)
	envelope, err := hex.DecodeString(envelopeHex)
	require.NoError(t, err)

	verifiedEnvelope, err := VerifyEnvelope(capsuleEmitCapsuleID, envelope)
	require.NoError(t, err)
	assert.True(t, verifiedEnvelope.OK)
	assert.Equal(t, keyID, hex.EncodeToString(verifiedEnvelope.PublicKey))

	tampered := decodeFixture(t, fixture)
	tampered["developer"] = "tampered-agent@v1"
	tamperedResult, err := VerifyCapsule(canonicalFixture(t, tampered))
	assert.ErrorContains(t, err, "capsule_id_mismatch")
	assert.False(t, tamperedResult.OK)

	corruptedEnvelope := append([]byte(nil), envelope...)
	corruptedEnvelope[len(corruptedEnvelope)-1] ^= 0xff
	localOnlyMutation := decodeFixture(t, fixture)
	localOnlyMutation["signature"] = hex.EncodeToString(corruptedEnvelope)
	localOnlyResult, err := VerifyCapsule(canonicalFixture(t, localOnlyMutation))
	require.NoError(t, err)
	assert.True(t, localOnlyResult.OK)
	require.NotNil(t, localOnlyResult.CapsuleID)
	assert.Equal(t, capsuleEmitCapsuleID, *localOnlyResult.CapsuleID)

	keyIDMutation := decodeFixture(t, fixture)
	keyIDMutation["key_id"] = "0000000000000000000000000000000000000000000000000000000000000000"
	keyIDMutationResult, err := VerifyCapsule(canonicalFixture(t, keyIDMutation))
	require.NoError(t, err)
	assert.True(t, keyIDMutationResult.OK)
	require.NotNil(t, keyIDMutationResult.CapsuleID)
	assert.Equal(t, capsuleEmitCapsuleID, *keyIDMutationResult.CapsuleID)

	corruptedResult, err := VerifyEnvelope(capsuleEmitCapsuleID, corruptedEnvelope)
	assert.ErrorContains(t, err, "envelope_signature_invalid")
	assert.False(t, corruptedResult.OK)
}

func TestCapsuleEmitSlotCompositionInteroperability(t *testing.T) {
	fixture, err := os.ReadFile(capsuleEmitSlotsFixturePath)
	require.NoError(t, err)
	digest := sha256.Sum256(fixture)
	require.Equal(t, capsuleEmitSlotsFixtureSHA, hex.EncodeToString(digest[:]))

	verified, err := VerifyCapsule(fixture)
	require.NoError(t, err)
	assert.True(t, verified.OK)
	require.NotNil(t, verified.CapsuleID)
	assert.Equal(t, capsuleEmitSlotsCompositionID, *verified.CapsuleID)

	stored := decodeFixture(t, fixture)
	compute := stored["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)
	members := compute["composed_members"].([]any)
	require.Len(t, members, 3)
	assert.Equal(t, "who", members[0].(map[string]any)["slot"])
	assert.Equal(t, "can", members[1].(map[string]any)["slot"])
	assert.Equal(t, capsuleEmitSlotsCarryCapsuleID, members[1].(map[string]any)["digest"])
	assert.Equal(t, "did", members[2].(map[string]any)["slot"])
}

func decodeFixture(t *testing.T, data []byte) map[string]any {
	t.Helper()
	decoded, err := DecodePayload(data)
	require.NoError(t, err)
	return decoded
}

func canonicalFixture(t *testing.T, value map[string]any) []byte {
	t.Helper()
	encoded, err := canonical.JCS(value)
	require.NoError(t, err)
	return encoded
}
