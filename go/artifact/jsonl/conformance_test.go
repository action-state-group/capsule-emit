package jsonl_test

import (
	"crypto/ed25519"
	jsonv2 "encoding/json/v2"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	jsonlstore "github.com/action-state-group/capsule-emit/go/artifact/jsonl"
	"github.com/stretchr/testify/require"
)

func TestConformance(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[string]{
		NewBacking: func(t *testing.T) string { return filepath.Join(t.TempDir(), "artifacts.jsonl") },
		Open: func(_ *testing.T, path, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return jsonlstore.New(path, namespace, trusted)
		},
		Capabilities: artifacttest.Capabilities{Durable: true},
	})
}

// TestConformanceLines pins the exact JSONL line for every line vector, in both
// directions, as Put writes and readAt parses it.
func TestConformanceLines(t *testing.T) {
	v, err := artifacttest.Load()
	require.NoError(t, err)
	require.NotEmpty(t, v.Lines)
	for _, line := range v.Lines {
		t.Run(line.Record, func(t *testing.T) {
			encoded, err := jsonv2.Marshal(v.Records[line.Record])
			require.NoError(t, err)
			require.Equal(t, line.Line, string(encoded))
			var decoded artifact.Record
			require.NoError(t, jsonv2.Unmarshal([]byte(line.Line), &decoded))
			require.Empty(t, artifacttest.Diff(v.Records[line.Record], decoded))
		})
	}
}
