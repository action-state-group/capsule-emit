package producer

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
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

	result, err := Seal(validInput(), identity)
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
