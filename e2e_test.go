package producer

import (
	"crypto/ed25519"
	"crypto/rand"
	"net/http"
	"testing"

	"github.com/ethanyzhang/capsule-producer-go/evidence"
	"github.com/ethanyzhang/capsule-producer-go/evidence/httpfact"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestHTTPFactsToVerifiedCapsuleEndToEnd(t *testing.T) {
	requestBody := []byte(`{"channel":"C123","text":"hello"}`)
	request, err := http.NewRequest(http.MethodPost, "https://slack.com/api/chat.postMessage", nil)
	require.NoError(t, err)
	requestFact, err := httpfact.CaptureRequest(request, requestBody, "slack-api")
	require.NoError(t, err)

	responseBody := []byte(`{"ok":true,"ts":"1722781258.001"}`)
	response := &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
	}
	responseFact, err := httpfact.CaptureResponse(response, responseBody, "ok", true)
	require.NoError(t, err)

	digests, err := evidence.DigestExchanges([]evidence.Exchange{{
		Provider:  "slack",
		Operation: "chat.postMessage",
		Request:   requestFact,
		Response:  responseFact,
	}})
	require.NoError(t, err)
	require.True(t, digests.ResponseKnown)

	input := validInput()
	input.Effect.RequestDigest = digests.RequestDigest
	input.Effect.ResponseDigest = digests.ResponseDigest
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	created, err := Create(input, identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)
	verified, err := VerifyStatement(created.Statement, verifier, identity.KeyID)
	require.NoError(t, err)

	effect := verified.Payload["effect"].(map[string]any)
	assert.Equal(t, digests.RequestDigest, effect["request_digest"])
	assert.Equal(t, digests.ResponseDigest, effect["response_digest"])
	assert.NotContains(t, string(created.Statement), "hello")
	assert.NotContains(t, string(created.Statement), "1722781258.001")
}
