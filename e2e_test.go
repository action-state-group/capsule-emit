package emit

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestJSONDigestsToVerifiedV4CapsuleEndToEnd(t *testing.T) {
	requestDigest, err := DigestJSON(struct {
		Channel string `json:"channel"`
		Text    string `json:"text"`
	}{Channel: "C123", Text: "hello"})
	require.NoError(t, err)
	responseDigest, err := DigestJSON(struct {
		OK bool   `json:"ok"`
		TS string `json:"ts"`
	}{OK: true, TS: "1722781258.001"})
	require.NoError(t, err)

	input := validInput()
	input.Effect.RequestDigest = requestDigest
	input.Effect.ResponseDigest = responseDigest
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	created, err := Seal(input, identity)
	require.NoError(t, err)
	class1, err := VerifyCapsule(created.Payload)
	require.NoError(t, err)
	assert.True(t, class1.OK)
	envelope, err := VerifyEnvelope(created.CapsuleID, created.Envelope)
	require.NoError(t, err)
	assert.Equal(t, []byte(publicKey), envelope.PublicKey)
	assert.NotContains(t, string(created.Payload), "hello")
	assert.NotContains(t, string(created.Payload), "1722781258.001")
	assert.NotContains(t, string(created.Envelope), "hello")
}
