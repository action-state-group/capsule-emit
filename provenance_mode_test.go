package emit

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// provenanceModeVectorDir holds byte-for-byte copies of the AAC -05
// provenance_mode conformance corpus (agent-action-capsule, branch
// aac-external-review-followups, commit 8def04c5,
// provenance-mode-vectors/<case>/{input,expected}.json), pinned by the
// upstream SHA256SUMS also copied into this directory. Class 1 check 9 (the
// gating provenance_mode verifier findings these vectors exercise) has not
// landed in the Go reference verifier (agent-action-capsule/go/verify) yet
// -- see the [capsule-emit-go-provenance-mode] outbox stanza. These tests
// cover the slice that does not depend on check 9: capsule_id byte-parity,
// proving provenance_mode's JCS/digest participation is identical to Python.
const provenanceModeVectorDir = "testdata/provenance-mode-vectors"

func TestProvenanceModeVectorsCapsuleIDByteParity(t *testing.T) {
	cases := []struct {
		name      string
		capsuleID string
	}{
		{"pos-provenance-mode-backfilled", "52bd075279c3528c2e08962a6e5948ad82e68fd079d8198e7a2a3c27d0883bd4"},
		{"neg-provenance-mode-time-rung-overclaim", "ec8bb8fed1f318cb20e606df4c5d700e5d5a766eb8b49601e1a4cd01277b1376"},
		{"neg-provenance-mode-backfilled-missing-fields", "e59414931d13cc91a2650482e6b3d27a9e0851c3eb6706c26996a28a19244804"},
		{"neg-provenance-mode-time-laundering", "9dbb97d6c1f7caf9b57a3a672ad82a294509376960920ed5c705adf3f99b9027"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join(provenanceModeVectorDir, tc.name, "input.json"))
			require.NoError(t, err)
			payload, err := DecodePayload(raw)
			require.NoError(t, err)
			require.Equal(t, tc.capsuleID, payload["capsule_id"], "vector fixture drifted from its pinned capsule_id")

			recomputed, err := canonical.ComputeCapsuleID(payload)
			require.NoError(t, err)
			assert.Equal(t, tc.capsuleID, recomputed, "Go capsule_id must match the Python reference byte-for-byte")
		})
	}
}

func TestProvenanceModeVectorStoreCapsuleIDByteParity(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(provenanceModeVectorDir, "pos-chain-duplicates-collapsed-once", "input.json"))
	require.NoError(t, err)
	decoded, err := DecodePayload(raw)
	require.NoError(t, err)
	ledger, ok := decoded["ledger"].([]any)
	require.True(t, ok, "store vector must carry a ledger array")
	require.Len(t, ledger, 2)

	expectedIDs := []string{
		"ed3cdeca453ee37e40bbeeee0f582f579873fa690a6af2d729578595b287d378",
		"5d7ee9fe6af6391a0a905b3297316bc483d781098d9ceca4fa697fa591f929dd",
	}
	for i, entry := range ledger {
		capsule, ok := entry.(map[string]any)
		require.True(t, ok)
		recomputed, err := canonical.ComputeCapsuleID(capsule)
		require.NoError(t, err)
		assert.Equal(t, expectedIDs[i], recomputed)
	}
	parentMode := ledger[0].(map[string]any)["provenance_mode"]
	assert.Nil(t, parentMode, "contemporaneous parent carries no provenance_mode block")
	childMode := ledger[1].(map[string]any)["provenance_mode"].(map[string]any)
	assert.Equal(t, "backfilled", childMode["mode"])
}

func TestBuildEmitsProvenanceModeBackfilled(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
	}
	built, err := Build(input)
	require.NoError(t, err)
	mode := built.Value["provenance_mode"].(map[string]any)
	assert.Equal(t, "backfilled", mode["mode"])
	assert.Equal(t, "import-2026-09", mode["import_batch"])
	assert.Equal(t, "2026-01-01T00:00:00Z", mode["source_asserted_at"])
	assert.Equal(t, "2026-09-22T00:00:00Z", mode["imported_at"])
	assert.NotContains(t, mode, "time_rung")
	sourceRef := mode["source_ref"].(map[string]any)
	assert.Equal(t, "x-external-ledger-entry", sourceRef["type"])
	assert.NotContains(t, sourceRef, "citation_purpose")

	recomputed, err := canonical.ComputeCapsuleID(built.Value)
	require.NoError(t, err)
	assert.Equal(t, built.CapsuleID, recomputed)
}

func TestBuildEmitsProvenanceModeBackfilledWithWitnessedTimeRung(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
		TimeRung:         TimeRungWitnessed,
	}
	built, err := Build(input)
	require.NoError(t, err)
	mode := built.Value["provenance_mode"].(map[string]any)
	assert.Equal(t, "witnessed", mode["time_rung"])
}

func TestBuildOmitsProvenanceModeWhenAbsent(t *testing.T) {
	input := validInput()
	built, err := Build(input)
	require.NoError(t, err)
	assert.NotContains(t, built.Value, "provenance_mode")
}

func TestBuildRejectsInvalidProvenanceModeValue(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{Mode: "com.example.imported"}
	_, err := Build(input)
	assert.ErrorContains(t, err, "provenance mode must be")
}

func TestBuildRejectsBackfilledMissingAllCompanionFields(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{Mode: ProvenanceModeBackfilled}
	_, err := Build(input)
	require.Error(t, err)
	for _, want := range []string{"source ref", "source asserted at", "import batch", "imported at"} {
		assert.ErrorContains(t, err, want)
	}
}

func TestBuildRejectsBackfilledMissingOneCompanionField(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		// ImportedAt deliberately omitted.
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "imported at")
}

func TestBuildRejectsBackfilledMalformedSourceRef(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry"}, // missing digest_alg, digest
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "source ref requires")
}

func TestBuildRejectsBackfilledSourceRefWithCitationPurpose(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest, CitationPurpose: "acted_on"},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "must not carry citation purpose")
}

func TestBuildRejectsContemporaneousWithCompanionFields(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:        ProvenanceModeContemporaneous,
		ImportBatch: "import-2026-09",
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "meaningful only when mode is")
}

func TestBuildRejectsContemporaneousWithTimeRung(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:     ProvenanceModeContemporaneous,
		TimeRung: TimeRungSelfAttested,
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "meaningful only when mode is")
}

func TestBuildRejectsInvalidTimeRungValue(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
		TimeRung:         "com.example.confirmed",
	}
	_, err := Build(input)
	assert.ErrorContains(t, err, "time rung must be")
}

func TestChainDuplicatesIsAKnownRegistryValue(t *testing.T) {
	assert.True(t, knownRegistries()["chain.relation"][string(ChainDuplicates)])
}
