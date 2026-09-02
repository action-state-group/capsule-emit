package emit

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type vectorManifest struct {
	Cases []struct {
		Name string `json:"name"`
	} `json:"cases"`
}

type vectorExpected struct {
	OK bool `json:"ok"`
}

func upstreamRepository(t *testing.T) string {
	t.Helper()
	path := os.Getenv("AAC_REPO")
	if path == "" {
		t.Skip("set AAC_REPO to an agent-action-capsule checkout to run frozen upstream vectors")
	}
	return path
}

func TestFrozenFormat4CapsuleVectors(t *testing.T) {
	repository := upstreamRepository(t)
	root := filepath.Join(repository, "test-vectors")
	manifestData, err := os.ReadFile(filepath.Join(root, "vectors.json"))
	require.NoError(t, err)
	var manifest vectorManifest
	require.NoError(t, json.Unmarshal(manifestData, &manifest))

	checked := 0
	for _, vector := range manifest.Cases {
		input, err := os.ReadFile(filepath.Join(root, vector.Name, "input.json"))
		require.NoError(t, err, vector.Name)
		var capsule map[string]any
		if json.Unmarshal(input, &capsule) != nil || capsule["format_version"] != "4" {
			continue
		}
		expectedData, err := os.ReadFile(filepath.Join(root, vector.Name, "expected.json"))
		require.NoError(t, err, vector.Name)
		var expected vectorExpected
		require.NoError(t, json.Unmarshal(expectedData, &expected), vector.Name)
		result, verifyErr := VerifyCapsule(input)
		assert.Equal(t, expected.OK, result.OK, vector.Name)
		assert.Equal(t, expected.OK, verifyErr == nil, vector.Name)
		checked++
	}
	assert.Greater(t, checked, 0)
}

func TestFrozenProducerEnvelopeVectors(t *testing.T) {
	repository := upstreamRepository(t)
	root := filepath.Join(repository, "producer-envelope-vectors")
	manifestData, err := os.ReadFile(filepath.Join(root, "vectors.json"))
	require.NoError(t, err)
	var manifest vectorManifest
	require.NoError(t, json.Unmarshal(manifestData, &manifest))
	require.Len(t, manifest.Cases, 8)

	for _, vector := range manifest.Cases {
		caseRoot := filepath.Join(root, vector.Name)
		capsuleID, err := os.ReadFile(filepath.Join(caseRoot, "capsule_id.txt"))
		require.NoError(t, err, vector.Name)
		envelope, err := os.ReadFile(filepath.Join(caseRoot, "envelope.cose"))
		require.NoError(t, err, vector.Name)
		expectedData, err := os.ReadFile(filepath.Join(caseRoot, "expected.json"))
		require.NoError(t, err, vector.Name)
		var expected vectorExpected
		require.NoError(t, json.Unmarshal(expectedData, &expected), vector.Name)
		result, verifyErr := VerifyEnvelope(string(bytes.TrimSpace(capsuleID)), envelope)
		assert.Equal(t, expected.OK, result.OK, vector.Name)
		assert.Equal(t, expected.OK, verifyErr == nil, vector.Name)
	}
}

func TestSignMatchesFrozenValidEnvelope(t *testing.T) {
	repository := upstreamRepository(t)
	seed := make([]byte, ed25519.SeedSize)
	for index := range seed {
		seed[index] = byte(index)
	}
	identity, err := NewEd25519SigningIdentity(ed25519.NewKeyFromSeed(seed))
	require.NoError(t, err)
	payload := make([]byte, 32)
	for index := range payload {
		payload[index] = byte(index + 32)
	}
	actual, err := signCapsuleID(payload, identity)
	require.NoError(t, err)
	expected, err := os.ReadFile(filepath.Join(repository, "producer-envelope-vectors", "valid", "envelope.cose"))
	require.NoError(t, err)
	assert.Equal(t, expected, actual)
}
