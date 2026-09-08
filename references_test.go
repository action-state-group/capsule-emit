package emit

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestBuildCrossRecordReferences(t *testing.T) {
	input := referenceInput()
	built, err := Build(input)
	require.NoError(t, err)
	t.Run("python-vector", func(t *testing.T) {
		root := upstreamRepository(t)
		data, err := os.ReadFile(filepath.Join(root, "go", "verify", "testdata", "references.json"))
		require.NoError(t, err)
		var vectors struct {
			Cases []struct {
				Name      string
				Canonical string
			}
		}
		require.NoError(t, json.Unmarshal(data, &vectors))
		expected := ""
		for _, vector := range vectors.Cases {
			if vector.Name == "external-capsule" {
				expected = vector.Canonical
			}
		}
		require.NotEmpty(t, expected)
		assert.Equal(t, expected, string(built.JSON))
	})
	verified, err := VerifyCapsule(built.JSON)
	require.NoError(t, err)
	require.Len(t, verified.Findings, 1)
	assert.Equal(t, "chain_check_store_level", verified.Findings[0].Code)
	input.References[0].CitationPurpose = "future-purpose"
	future, err := Build(input)
	require.NoError(t, err)
	verified, err = VerifyCapsule(future.JSON)
	require.NoError(t, err)
	require.Len(t, verified.Findings, 2)
	assert.Equal(t, "unknown_registry_value", verified.Findings[1].Code)
	assert.Equal(t, "info", verified.Findings[1].Severity)
	tampered := strings.Replace(string(built.JSON), "responds_to", "acted_on", 1)
	_, err = VerifyCapsule([]byte(tampered))
	assert.ErrorContains(t, err, "capsule_id_mismatch")
	input = referenceInput()
	input.References[0].LogCoordinates = map[string]any{"log_id": "example", "leaf_index": 0, "inclusion_proof": "opaque"}
	withCoordinates, err := Build(input)
	require.NoError(t, err)
	assert.Contains(t, string(withCoordinates.JSON), `"inclusion_proof":"opaque"`)
	input.References = []Reference{}
	empty, err := Build(input)
	require.NoError(t, err)
	assert.Contains(t, string(empty.JSON), `"references":[]`)
	input.References = nil
	absent, err := Build(input)
	require.NoError(t, err)
	assert.NotContains(t, string(absent.JSON), `"references"`)
	assert.NotEqual(t, absent.CapsuleID, empty.CapsuleID, "format-4 JCS preserves the difference between absent and empty")
}

// referenceInput matches the Python-authored external-capsule fixture.
func referenceInput() Input {
	return Input{ActionID: "reference/example", ActionType: ActionTypeFYI, Operator: "example-org", Developer: "example-agent@v1", Timestamp: time.Date(2026, 9, 7, 12, 0, 0, 0, time.UTC), Chain: &Chain{ParentCapsuleID: strings.Repeat("a", 64), Relation: ChainConfirms}, References: []Reference{{Type: "agent-action-capsule", DigestAlg: "SHA-256", Digest: strings.Repeat("b", 64), CitationPurpose: "responds_to"}}}
}

func TestBuildRejectsInvalidReferences(t *testing.T) {
	for _, test := range []struct {
		name     string
		mutate   func(*Input)
		contains string
	}{
		{"duplicate parent", func(input *Input) { input.References[0].Digest = input.Chain.ParentCapsuleID }, "reference_duplicates_chain_parent"},
		{"missing type", func(input *Input) { input.References[0].Type = "" }, "reference_malformed"},
		{"unmarshalable coordinates", func(input *Input) {
			input.References[0].LogCoordinates = map[string]any{"log_id": "example", "leaf_index": 0, "inclusion_proof": make(chan int)}
		}, "marshal JSON digest input"},
		{"missing proof", func(input *Input) {
			input.References[0].LogCoordinates = map[string]any{"log_id": "example", "leaf_index": 0}
		}, "reference_log_coordinates_malformed"},
	} {
		t.Run(test.name, func(t *testing.T) {
			input := referenceInput()
			test.mutate(&input)
			_, err := Build(input)
			assert.ErrorContains(t, err, test.contains)
		})
	}
}
