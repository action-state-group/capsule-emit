package emit

import (
	"crypto/ed25519"
	"encoding/json"
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
// upstream SHA256SUMS also copied into this directory. The copies are
// released and never rewritten. Class 1 check 9 has since landed in the Go
// reference verifier, so these tests cover both capsule_id byte-parity and
// the committed verdicts.
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

func backfilledProvenanceMode() *ProvenanceMode {
	return &ProvenanceMode{
		Mode:             ProvenanceModeBackfilled,
		SourceRef:        &Reference{Type: "x-external-ledger-entry", DigestAlg: "SHA-256", Digest: parentDigest},
		SourceAssertedAt: "2026-01-01T00:00:00Z",
		ImportBatch:      "import-2026-09",
		ImportedAt:       "2026-09-22T00:00:00Z",
	}
}

func corroboratingTimeReference() Reference {
	return Reference{
		Type:            "x-transparency-receipt",
		DigestAlg:       "SHA-256",
		Digest:          "4444444444444444444444444444444444444444444444444444444444444444",
		CitationPurpose: "corroborates_source_time",
	}
}

// TestBuildEmitsWitnessedTimeRungWhenCorroborated: check 9 lets a backfilled
// Capsule claim time_rung "witnessed" only with a well-formed references[]
// entry citing corroborates_source_time; with one, Build emits the claim and
// the verifier rederives the same rung from that evidence.
func TestBuildEmitsWitnessedTimeRungWhenCorroborated(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.TimeRung = TimeRungWitnessed
	input.References = []Reference{corroboratingTimeReference()}
	built, err := Build(input)
	require.NoError(t, err)
	mode := built.Value["provenance_mode"].(map[string]any)
	assert.Equal(t, "witnessed", mode["time_rung"])

	verified, err := VerifyCapsule(built.JSON)
	require.NoError(t, err)
	assert.Equal(t, "witnessed", verified.Assurance["provenance_time_rung"])
}

func requireCheck9Refusal(t *testing.T, err error, code string) {
	t.Helper()
	var class1 *Class1Error
	require.ErrorAs(t, err, &class1)
	codes := make([]string, 0, len(class1.Findings))
	for _, finding := range class1.Findings {
		if finding.Severity == "error" {
			require.NotNil(t, finding.Check)
			assert.Equal(t, 9, *finding.Check)
			codes = append(codes, finding.Code)
		}
	}
	assert.Equal(t, []string{code}, codes)
}

func sealIdentityForTest(t *testing.T) SigningIdentity {
	t.Helper()
	seed := make([]byte, ed25519.SeedSize)
	identity, err := NewEd25519SigningIdentity(ed25519.NewKeyFromSeed(seed))
	require.NoError(t, err)
	return identity
}

// A producer must never emit a timing overclaim: Build and Seal refuse a
// witnessed time rung that no corroborates_source_time reference supports.
func TestBuildRefusesUncorroboratedWitnessedTimeRung(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.TimeRung = TimeRungWitnessed
	_, err := Build(input)
	requireCheck9Refusal(t, err, "provenance_time_rung_overclaim")
}

func TestSealRefusesUncorroboratedWitnessedTimeRung(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.TimeRung = TimeRungWitnessed
	result, err := Seal(SealInput{Capsule: input, Identity: sealIdentityForTest(t)})
	requireCheck9Refusal(t, err, "provenance_time_rung_overclaim")
	assert.Empty(t, result.Payload)
	assert.Empty(t, result.Envelope)
}

// A reference under any other purpose never corroborates source time.
func TestBuildRefusesWitnessedTimeRungCitedUnderAnotherPurpose(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.TimeRung = TimeRungWitnessed
	reference := corroboratingTimeReference()
	reference.CitationPurpose = "acted_on"
	input.References = []Reference{reference}
	_, err := Build(input)
	requireCheck9Refusal(t, err, "provenance_time_rung_overclaim")
}

// Build and Seal refuse the laundering shape: imported_at byte-equal to
// source_asserted_at on a backfilled record.
func TestBuildRefusesTimeLaunderingShape(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.ImportedAt = input.ProvenanceMode.SourceAssertedAt
	_, err := Build(input)
	requireCheck9Refusal(t, err, "provenance_time_laundering_shape")
}

func TestSealRefusesTimeLaunderingShape(t *testing.T) {
	input := validInput()
	input.ProvenanceMode = backfilledProvenanceMode()
	input.ProvenanceMode.ImportedAt = input.ProvenanceMode.SourceAssertedAt
	result, err := Seal(SealInput{Capsule: input, Identity: sealIdentityForTest(t)})
	requireCheck9Refusal(t, err, "provenance_time_laundering_shape")
	assert.Empty(t, result.Payload)
	assert.Empty(t, result.Envelope)
}

// TestProvenanceModeVectorsVerifyAsCommitted checks VerifyCapsule against the
// committed AAC verdicts: the positive verifies, and each negative fails with
// exactly its committed check-9 finding codes.
func TestProvenanceModeVectorsVerifyAsCommitted(t *testing.T) {
	for _, name := range []string{
		"pos-provenance-mode-backfilled",
		"neg-provenance-mode-time-rung-overclaim",
		"neg-provenance-mode-backfilled-missing-fields",
		"neg-provenance-mode-time-laundering",
	} {
		t.Run(name, func(t *testing.T) {
			input, err := os.ReadFile(filepath.Join(provenanceModeVectorDir, name, "input.json"))
			require.NoError(t, err)
			expectedData, err := os.ReadFile(filepath.Join(provenanceModeVectorDir, name, "expected.json"))
			require.NoError(t, err)
			var expected struct {
				OK       bool `json:"ok"`
				Findings []struct {
					Code string `json:"code"`
				} `json:"findings"`
			}
			require.NoError(t, json.Unmarshal(expectedData, &expected))
			result, verifyErr := VerifyCapsule(input)
			assert.Equal(t, expected.OK, result.OK)
			assert.Equal(t, expected.OK, verifyErr == nil)
			wantCodes := make([]string, 0, len(expected.Findings))
			for _, finding := range expected.Findings {
				wantCodes = append(wantCodes, finding.Code)
			}
			gotCodes := make([]string, 0, len(result.Findings))
			for _, finding := range result.Findings {
				gotCodes = append(gotCodes, finding.Code)
			}
			assert.Equal(t, wantCodes, gotCodes)
		})
	}
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
