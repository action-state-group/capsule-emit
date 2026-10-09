package artifact_test

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	emit "github.com/action-state-group/capsule-emit-go"
	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// authoredInteropRecord loads the committed "authored" record from one frozen
// interop pack, with its original payload bound as an artifact.
func authoredInteropRecord(t *testing.T, pack string) (artifact.Record, ed25519.PublicKey) {
	t.Helper()
	caseDir := filepath.Join("..", "testdata", "capsule-emit", pack, "valid", "authored")
	capsule, err := os.ReadFile(filepath.Join(caseDir, "capsule.detached.jcs"))
	require.NoError(t, err)
	envelope, err := os.ReadFile(filepath.Join(caseDir, "envelope.cose"))
	require.NoError(t, err)
	expectedData, err := os.ReadFile(filepath.Join(caseDir, "expected.json"))
	require.NoError(t, err)
	var expected struct {
		CapsuleID    string `json:"capsule_id"`
		PublicKeyHex string `json:"public_key_hex"`
	}
	require.NoError(t, json.Unmarshal(expectedData, &expected))
	publicKey, err := hex.DecodeString(expected.PublicKeyHex)
	require.NoError(t, err)
	return artifact.Record{
		CapsuleID:        expected.CapsuleID,
		Capsule:          capsule,
		ProducerEnvelope: envelope,
		Artifacts: []artifact.Artifact{{
			Name: "payload", Binding: artifact.PayloadDigest,
			Content: []byte(`{"task":"summarize","ticket":105}`), State: artifact.Present,
		}},
	}, publicKey
}

// TestReleasedV04RecordAndV05TwinBothLoadThroughRecord: the committed -04
// record and its -05 twin both pass Prepare and Verify, and each bound
// original rehashes to the Capsule's committed digest.
func TestReleasedV04RecordAndV05TwinBothLoadThroughRecord(t *testing.T) {
	for _, pack := range []string{"format4-interop", "format4-interop-v05"} {
		t.Run(pack, func(t *testing.T) {
			record, publicKey := authoredInteropRecord(t, pack)
			prepared, err := artifact.Prepare(record)
			require.NoError(t, err)
			checks, err := artifact.Verify(prepared, []ed25519.PublicKey{publicKey})
			require.NoError(t, err)
			assert.True(t, checks["payload"].Bound)
			assert.True(t, checks["payload"].Verified)
		})
	}
}

// TestRecordWithUnrecognizedSpecVersionIsNotRejected: a correctly signed
// record whose spec_version names no published revision still verifies.
func TestRecordWithUnrecognizedSpecVersionIsNotRejected(t *testing.T) {
	record, _ := authoredInteropRecord(t, "format4-interop-v05")
	payload, err := emit.DecodePayload(record.Capsule)
	require.NoError(t, err)
	payload["spec_version"] = "draft-mih-scitt-agent-action-capsule-99"
	delete(payload, "capsule_id")
	capsuleID, err := canonical.ComputeCapsuleID(payload)
	require.NoError(t, err)
	payload["capsule_id"] = capsuleID
	data, err := canonical.JCS(payload)
	require.NoError(t, err)

	publicKey, privateKey, err := ed25519.GenerateKey(nil)
	require.NoError(t, err)
	identity, err := emit.NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	envelope, err := emit.Sign(emit.BuiltPayload{CapsuleID: capsuleID, Value: payload, JSON: data}, identity)
	require.NoError(t, err)

	record.CapsuleID = capsuleID
	record.Capsule = data
	record.ProducerEnvelope = envelope
	checks, err := artifact.Verify(record, []ed25519.PublicKey{publicKey})
	require.NoError(t, err)
	assert.True(t, checks["payload"].Verified)
}
