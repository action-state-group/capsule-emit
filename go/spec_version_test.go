package emit

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/action-state-group/agent-action-capsule/go/verify"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const unrecognizedSpecVersion = "draft-mih-scitt-agent-action-capsule-99"

func TestSpecVersionConstantsMirrorAACReference(t *testing.T) {
	assert.Equal(t, "draft-mih-scitt-agent-action-capsule-05", SpecVersion)
	assert.Equal(t, verify.CurrentSpecVersion, SpecVersion)
	assert.Equal(t, verify.AcceptedSpecVersions, AcceptedSpecVersions)
	assert.Equal(t, []string{
		"draft-mih-scitt-agent-action-capsule-04",
		"draft-mih-scitt-agent-action-capsule-05",
	}, AcceptedSpecVersions)
}

func TestBuildSealReceivedAndCompositionStampSpecVersion05(t *testing.T) {
	built, err := Build(validInput())
	require.NoError(t, err)
	assert.Equal(t, "draft-mih-scitt-agent-action-capsule-05", built.Value["spec_version"])

	received, err := Received(validInput(), []byte("opaque"), "machine-mandate")
	require.NoError(t, err)
	assert.Equal(t, "draft-mih-scitt-agent-action-capsule-05", received.Value["spec_version"])

	composed, err := BuildComposition(validInput(), Who(built))
	require.NoError(t, err)
	assert.Equal(t, "draft-mih-scitt-agent-action-capsule-05", composed.Value["spec_version"])

	sealed, err := Seal(SealInput{Capsule: validInput(), Payload: map[string]any{"k": "v"}, Identity: sealIdentityForTest(t)})
	require.NoError(t, err)
	payload, err := DecodePayload(sealed.Payload)
	require.NoError(t, err)
	assert.Equal(t, "draft-mih-scitt-agent-action-capsule-05", payload["spec_version"])
}

// TestReleasedV04CapsulesAndV05TwinsBothVerify: every committed -04 record in
// the released interop pack and its -05 twin verify alike; the twins carry
// different Capsule IDs because spec_version is committed by the ID.
func TestReleasedV04CapsulesAndV05TwinsBothVerify(t *testing.T) {
	for _, name := range []string{"authored", "received", "who", "did", "composition"} {
		t.Run(name, func(t *testing.T) {
			ids := make([]string, 0, len(format4InteropPacks))
			for _, pack := range format4InteropPacks {
				for _, file := range []string{"capsule.detached.jcs", "capsule.stored.json"} {
					data, err := os.ReadFile(filepath.Join(pack.root, "valid", name, file))
					require.NoError(t, err)
					result, err := VerifyCapsule(data)
					require.NoError(t, err, "%s %s", pack.specVersion, file)
					require.True(t, result.OK)
					payload, err := DecodePayload(data)
					require.NoError(t, err)
					assert.Equal(t, pack.specVersion, payload["spec_version"])
				}
				data, err := os.ReadFile(filepath.Join(pack.root, "valid", name, "capsule.detached.jcs"))
				require.NoError(t, err)
				result, err := VerifyCapsule(data)
				require.NoError(t, err)
				ids = append(ids, *result.CapsuleID)
			}
			assert.NotEqual(t, ids[0], ids[1])
		})
	}
}

// TestVerifyCapsuleNeverRejectsOnSpecVersionAlone: -04, -05 and an
// unrecognized revision reach Class 1 identically, with no error finding.
func TestVerifyCapsuleNeverRejectsOnSpecVersionAlone(t *testing.T) {
	for _, specVersion := range []string{
		"draft-mih-scitt-agent-action-capsule-04",
		"draft-mih-scitt-agent-action-capsule-05",
		unrecognizedSpecVersion,
	} {
		t.Run(specVersion, func(t *testing.T) {
			input := validInput()
			input.specVersion = specVersion
			built, err := Build(input)
			require.NoError(t, err)
			result, err := VerifyCapsule(built.JSON)
			require.NoError(t, err)
			assert.True(t, result.OK)
			for _, finding := range result.Findings {
				assert.NotEqual(t, "error", finding.Severity, finding)
			}
		})
	}
}

// TestUnrecognizedSpecVersionSealsAndComposes: a signed record carrying an
// unrecognized spec_version verifies, envelope included, and is accepted as a
// composition member.
func TestUnrecognizedSpecVersionSealsAndComposes(t *testing.T) {
	input := validInput()
	input.specVersion = unrecognizedSpecVersion
	sealed, err := Seal(SealInput{Capsule: input, Payload: map[string]any{"k": "v"}, Identity: sealIdentityForTest(t)})
	require.NoError(t, err)
	_, err = VerifyEnvelope(sealed.CapsuleID, sealed.Envelope)
	require.NoError(t, err)

	composed, err := BuildComposition(validInput(), Did(sealed))
	require.NoError(t, err)
	assert.Equal(t, SpecVersion, composed.Value["spec_version"])
}

// TestVerifyCapsuleStillRejectsWrongFormatOrCanonicalization: dropping the
// spec_version comparison leaves the format_version and canonicalization_id
// gate intact.
func TestVerifyCapsuleStillRejectsWrongFormatOrCanonicalization(t *testing.T) {
	for field, value := range map[string]string{"format_version": "3", "canonicalization_id": "other"} {
		t.Run(field, func(t *testing.T) {
			built, err := Build(validInput())
			require.NoError(t, err)
			// The profile gate runs before Class 1, so capsule_id is left stale.
			payload := built.Value
			payload[field] = value
			data, err := canonical.JCS(payload)
			require.NoError(t, err)
			_, err = VerifyCapsule(data)
			assert.ErrorContains(t, err, "unsupported Capsule profile")
		})
	}
}
