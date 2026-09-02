package emit

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestSealAndIndependentVerification(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)

	result, err := Seal(SealInput{
		Capsule:  validInput(),
		Payload:  map[string]any{"request": "test"},
		Identity: identity,
	})
	require.NoError(t, err)
	assert.NotEmpty(t, result.Payload)
	assert.NotEmpty(t, result.Envelope)

	class1, err := VerifyCapsule(result.Payload)
	require.NoError(t, err)
	assert.True(t, class1.OK)
	envelope, err := VerifyEnvelope(result.CapsuleID, result.Envelope)
	require.NoError(t, err)
	assert.True(t, envelope.OK)
	assert.Equal(t, []byte(publicKey), envelope.PublicKey)
}

func TestSealRawPayloadMapsModelAndComputeAttestation(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)

	payload := map[string]any{"repository": "example/repo", "issue": 42}
	output := map[string]any{"scope": "scheduler", "confidence": 5}
	result, err := Seal(SealInput{
		Capsule:     validInput(),
		Payload:     payload,
		AgentOutput: output,
		Model:       &Model{Provider: "anthropic", ModelID: "claude-sonnet-4-6"},
		Runtime:     "alchemy",
		Identity:    identity,
	})
	require.NoError(t, err)

	decoded, err := DecodePayload(result.Payload)
	require.NoError(t, err)
	model := decoded["model_attestation"].(map[string]any)
	assert.Equal(t, "anthropic", model["provider"])
	assert.Equal(t, "claude-sonnet-4-6", model["model_id"])
	compute := model["compute_attestation"].(map[string]any)
	expectedInputDigest, err := DigestJSON(payload)
	require.NoError(t, err)
	expectedOutputDigest, err := DigestJSON(output)
	require.NoError(t, err)
	assert.Equal(t, expectedInputDigest, compute["agent_input_digest"])
	assert.Equal(t, expectedOutputDigest, compute["agent_output_digest"])
	assert.Equal(t, "alchemy", compute["runtime"])

	// Effect commitments remain application-owned and are not replaced by the
	// general agent input/output commitments.
	effect := decoded["effect"].(map[string]any)
	assert.Equal(t, requestDigest, effect["request_digest"])
	assert.Equal(t, responseDigest, effect["response_digest"])
}

func TestSealSupportsJSONNullAndDidComposition(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)

	nullResult, err := Seal(SealInput{
		Capsule:     validInput(),
		Payload:     json.RawMessage("null"),
		AgentOutput: json.RawMessage("null"),
		Identity:    identity,
	})
	require.NoError(t, err)
	nullPayload, err := DecodePayload(nullResult.Payload)
	require.NoError(t, err)
	nullCompute := nullPayload["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)
	nullDigest, err := DigestJSON(nil)
	require.NoError(t, err)
	assert.Equal(t, nullDigest, nullCompute["agent_input_digest"])
	assert.Equal(t, nullDigest, nullCompute["agent_output_digest"])

	memberInput := validInput()
	memberInput.ActionID = "did-member-123"
	member, err := Seal(SealInput{
		Capsule:  memberInput,
		Payload:  map[string]any{"action": "publish"},
		Identity: identity,
	})
	require.NoError(t, err)
	compositionInput := validInput()
	compositionInput.ActionID = "composition-123"
	composed, err := Seal(SealInput{
		Capsule:  compositionInput,
		Members:  []SlotMember{Did(member)},
		Runtime:  "alchemy",
		Identity: identity,
	})
	require.NoError(t, err)
	composedPayload, err := DecodePayload(composed.Payload)
	require.NoError(t, err)
	composedCompute := composedPayload["model_attestation"].(map[string]any)["compute_attestation"].(map[string]any)
	assert.Equal(t, "alchemy", composedCompute["runtime"])
	members := composedCompute["composed_members"].([]any)
	require.Len(t, members, 1)
	assert.Equal(t, "did", members[0].(map[string]any)["slot"])
	assert.Equal(t, member.CapsuleID, members[0].(map[string]any)["digest"])
}

func TestSealOmitsAbsentInputAndRejectsEmptyComposition(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)

	result, err := Seal(SealInput{Capsule: validInput(), Identity: identity})
	require.NoError(t, err)
	decoded, err := DecodePayload(result.Payload)
	require.NoError(t, err)
	assert.NotContains(t, decoded, "model_attestation")

	var typedNilPointer *struct{ Value string }
	var typedNilSlice []string
	typedNilResult, err := Seal(SealInput{
		Capsule:     validInput(),
		Payload:     typedNilPointer,
		AgentOutput: typedNilSlice,
		Identity:    identity,
	})
	require.NoError(t, err)
	typedNilPayload, err := DecodePayload(typedNilResult.Payload)
	require.NoError(t, err)
	assert.NotContains(t, typedNilPayload, "model_attestation")

	var typedNilChannel chan string
	_, err = Seal(SealInput{Capsule: validInput(), Payload: typedNilChannel, Identity: identity})
	assert.ErrorContains(t, err, "digest agent input")

	var typedNilFunction func()
	_, err = Seal(SealInput{Capsule: validInput(), AgentOutput: typedNilFunction, Identity: identity})
	assert.ErrorContains(t, err, "digest agent output")

	_, err = Seal(SealInput{
		Capsule:  validInput(),
		Members:  []SlotMember{},
		Identity: identity,
	})
	assert.ErrorContains(t, err, "at least one slot member")
}

func TestSealRejectsAmbiguousAndInvalidInputs(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)

	tests := []struct {
		name     string
		input    SealInput
		contains string
	}{
		{
			name: "low-level attestation mixed with high-level fields",
			input: SealInput{
				Capsule:  func() Input { value := validInput(); value.Model = &Model{Provider: "x"}; return value }(),
				Identity: identity,
			},
			contains: "must use Model and Runtime",
		},
		{
			name: "composition payload",
			input: SealInput{
				Capsule:  validInput(),
				Payload:  map[string]any{"unexpected": true},
				Members:  []SlotMember{Did(mustBuild(t, validInput()))},
				Identity: identity,
			},
			contains: "must not include payload",
		},
		{
			name: "invalid input JSON",
			input: SealInput{
				Capsule:  validInput(),
				Payload:  failingDigestJSON{},
				Identity: identity,
			},
			contains: "digest agent input",
		},
		{
			name: "invalid output JSON",
			input: SealInput{
				Capsule:     validInput(),
				Payload:     map[string]any{"ok": true},
				AgentOutput: failingDigestJSON{},
				Identity:    identity,
			},
			contains: "digest agent output",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := Seal(test.input)
			assert.ErrorContains(t, err, test.contains)
		})
	}
}

func mustBuild(t *testing.T, input Input) BuiltPayload {
	t.Helper()
	built, err := Build(input)
	require.NoError(t, err)
	return built
}

func TestVerifyCapsuleNormalizesNegativeZeroExtension(t *testing.T) {
	built, err := Build(validInput())
	require.NoError(t, err)
	payload, err := DecodePayload(built.JSON)
	require.NoError(t, err)

	payload["extension_count"] = json.Number("0")
	capsuleID, err := canonical.ComputeCapsuleID(payload)
	require.NoError(t, err)
	payload["capsule_id"] = capsuleID
	payload["extension_count"] = json.Number("-0")
	encoded, err := canonical.JCS(payload)
	require.NoError(t, err)

	result, err := VerifyCapsule(encoded)
	require.NoError(t, err)
	assert.True(t, result.OK)
}

func TestSignSupportsMultipleIndependentEnvelopes(t *testing.T) {
	built, err := Build(validInput())
	require.NoError(t, err)
	seen := make(map[string]bool)
	for range 2 {
		publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
		require.NoError(t, err)
		identity, err := NewEd25519SigningIdentity(privateKey)
		require.NoError(t, err)
		envelopeBytes, err := Sign(built, identity)
		require.NoError(t, err)
		verified, err := VerifyEnvelope(built.CapsuleID, envelopeBytes)
		require.NoError(t, err)
		assert.Equal(t, []byte(publicKey), verified.PublicKey)
		seen[hex.EncodeToString(verified.PublicKey)] = true
	}
	assert.Len(t, seen, 2)
}

func TestV4OnlyAndSigningErrors(t *testing.T) {
	_, err := NewEd25519SigningIdentity(ed25519.PrivateKey("short"))
	assert.Error(t, err)
	_, err = NewSigningIdentity(nil, make(ed25519.PublicKey, ed25519.PublicKeySize))
	assert.Error(t, err)
	built, err := Build(validInput())
	require.NoError(t, err)
	_, err = Sign(built, SigningIdentity{})
	assert.Error(t, err)
	identity, err := NewEd25519SigningIdentity(ed25519.NewKeyFromSeed(make([]byte, ed25519.SeedSize)))
	require.NoError(t, err)
	malformed := built
	malformed.CapsuleID = "ABC"
	_, err = Sign(malformed, identity)
	assert.ErrorContains(t, err, "lowercase")
	mismatched := built
	mismatched.CapsuleID = requestDigest
	_, err = Sign(mismatched, identity)
	assert.ErrorContains(t, err, "does not match")
	otherPublic, _, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	wrongPair, err := NewSigningIdentity(identity.signer, otherPublic)
	require.NoError(t, err)
	_, err = Sign(built, wrongPair)
	assert.ErrorContains(t, err, "does not match public key")

	legacy := append([]byte(nil), built.JSON...)
	legacy = []byte(string(legacy))
	payload, err := DecodePayload(legacy)
	require.NoError(t, err)
	payload["format_version"] = "2"
	delete(payload, "canonicalization_id")
	encoded, err := jsonBytes(payload)
	require.NoError(t, err)
	_, err = VerifyCapsule(encoded)
	assert.ErrorContains(t, err, "only AAC format 4")

	_, err = VerifyEnvelope(built.CapsuleID, []byte("bad"))
	assert.Error(t, err)
}

func jsonBytes(value map[string]any) ([]byte, error) {
	return canonical.JCS(value)
}
