package httpfact

import (
	"crypto/sha256"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestCaptureRequestCreatesImmutableMinimalSnapshot(t *testing.T) {
	httpRequest, err := http.NewRequest(http.MethodPost, "https://slack.com/api/chat.postMessage", nil)
	require.NoError(t, err)
	body := []byte(`{"channel":"C1","text":"private"}`)
	fact, err := CaptureRequest(httpRequest, body, "slack-api")
	require.NoError(t, err)

	httpRequest.Method = http.MethodDelete
	body[0] = 'X'
	evidence, err := fact.RequestEvidence()
	require.NoError(t, err)
	assert.Equal(t, http.MethodPost, evidence["method"])
	assert.Equal(t, "slack-api", evidence["target_class"])
	assert.Equal(t, json.Number("33"), evidence["body_length"])
	assert.Equal(t, "f6bcb8ca5c5124b09b1a5778e04bf944216a93f7268b1b5cf6475c8c69d3e741", evidence["content_digest"])
}

func TestCaptureResponseNormalizesMetadata(t *testing.T) {
	response := &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json; charset=utf-8"}},
	}
	fact, err := CaptureResponse(response, []byte(`{"ok":true}`), " invalid provider code ", true)
	require.NoError(t, err)
	evidence, err := fact.ResponseEvidence()
	require.NoError(t, err)
	assert.Equal(t, json.Number("200"), evidence["status_code"])
	assert.Equal(t, "application/json", evidence["media_type"])
	assert.Equal(t, "provider_error", evidence["provider_code"])
	assert.Equal(t, true, evidence["accepted"])
	assert.True(t, fact.ResponseObserved())
}

func TestHTTPFactConstructorsRejectInvalidInput(t *testing.T) {
	digest := sha256.Sum256([]byte("body"))
	longCode := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	_, err := CaptureRequest(nil, nil, "api")
	require.ErrorContains(t, err, "required")
	_, err = NewRequestWithDigest("", "api", 1, digest[:])
	require.ErrorContains(t, err, "method")
	_, err = NewRequestWithDigest(http.MethodPost, "api", -1, digest[:])
	require.ErrorContains(t, err, "length")
	_, err = NewRequestWithDigest(http.MethodPost, "api", 1, digest[:4])
	require.ErrorContains(t, err, "SHA-256")

	_, err = CaptureResponse(nil, nil, "", false)
	require.ErrorContains(t, err, "required")
	_, err = NewResponseWithDigest(99, "", "", 1, digest[:], false)
	require.ErrorContains(t, err, "status code")
	_, err = NewResponseWithDigest(600, "", "", 1, digest[:], false)
	require.ErrorContains(t, err, "status code")
	_, err = NewResponseWithDigest(200, "", "", -1, digest[:], false)
	require.ErrorContains(t, err, "length")
	_, err = NewResponseWithDigest(200, "", "", 1, nil, false)
	require.ErrorContains(t, err, "SHA-256")

	fact, err := NewResponseWithDigest(200, longCode, "text/plain", 1, digest[:], true)
	require.NoError(t, err)
	assert.Equal(t, "provider_error", fact.ProviderCode)
}
