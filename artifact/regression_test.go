package artifact_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"strings"
	"testing"
	"time"

	emit "github.com/action-state-group/capsule-emit-go"
	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Freeze the v1 storage encoding independently of Capsule construction/signing.
// Opaque synthetic bytes keep producer changes from moving this golden value.
func TestStorageChecksumGolden(t *testing.T) {
	record, err := artifact.Prepare(artifact.Record{
		CapsuleID: strings.Repeat("a", 64), Capsule: []byte("{}"), ProducerEnvelope: []byte{0, 1, 2, 255},
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: []byte("{}\n"), State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, State: artifact.NeverRetained},
		},
	})
	require.NoError(t, err)
	// The golden alone cannot notice a new, empty omitempty field. These
	// compile-time shapes pin names, types and JSON tags for storage-format review;
	// changing the persisted preimage also requires a migration plan.
	var _ struct {
		CapsuleID        string              `json:"capsule_id"`
		Capsule          []byte              `json:"capsule"`
		ProducerEnvelope []byte              `json:"producer_envelope"`
		Artifacts        []artifact.Artifact `json:"artifacts"`
	} = record
	var _ struct {
		Name          string                  `json:"name"`
		Binding       artifact.DigestField    `json:"binding,omitempty"`
		Content       []byte                  `json:"content,omitempty"`
		State         artifact.RetentionState `json:"state"`
		ContentSHA256 string                  `json:"content_sha256,omitempty"`
	} = record.Artifacts[0]
	const golden = "ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9"
	checksum, err := record.StorageChecksum()
	require.NoError(t, err)
	assert.Equal(t, golden, checksum)
	record.Artifacts[0].Content = nil
	record.Artifacts[0].State = artifact.Purged
	checksum, err = record.StorageChecksum()
	require.NoError(t, err)
	assert.Equal(t, golden, checksum)
}

func TestVerifyIdentityAndRawIntegrity(t *testing.T) {
	record, pub := testutil.Record(t, "identity-regression")
	other, _ := testutil.Record(t, "other-identity")
	prepared, err := artifact.Prepare(record)
	require.NoError(t, err)
	for _, test := range []struct {
		name   string
		mutate func(*artifact.Record)
		want   error
	}{
		{"valid_wrong_id", func(r *artifact.Record) { r.CapsuleID = other.CapsuleID }, artifact.ErrInvalid},
		{"different_capsule", func(r *artifact.Record) { r.Capsule = other.Capsule }, artifact.ErrInvalid},
		{"different_envelope", func(r *artifact.Record) { r.ProducerEnvelope = other.ProducerEnvelope }, artifact.ErrInvalid},
		{"unbound_bytes", func(r *artifact.Record) { r.Artifacts[2].Content = []byte("changed attachment") }, artifact.ErrCorrupt},
	} {
		t.Run(test.name, func(t *testing.T) {
			copy := prepared
			copy.Artifacts = append([]artifact.Artifact(nil), prepared.Artifacts...)
			test.mutate(&copy)
			_, err := artifact.Verify(copy, []ed25519.PublicKey{pub})
			require.ErrorIs(t, err, test.want)
		})
	}
}

func TestVerifyEffectBindings(t *testing.T) {
	pub, private, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := emit.NewEd25519SigningIdentity(private)
	require.NoError(t, err)
	request, response := []byte(`{"operation":"synthetic"}`), []byte(`{"ok":true}`)
	requestDigest, err := emit.DigestJSON(json.RawMessage(request))
	require.NoError(t, err)
	responseDigest, err := emit.DigestJSON(json.RawMessage(response))
	require.NoError(t, err)
	sealed, err := emit.Seal(emit.SealInput{
		Capsule: emit.Input{
			ActionID: "effect-bindings", ActionType: emit.ActionTypeDecide, Operator: "test", Developer: "test",
			Timestamp:   time.Date(2026, 9, 8, 0, 0, 0, 0, time.UTC),
			Disposition: &emit.Disposition{Decision: emit.DecisionAccept, Approver: emit.ApproverPolicy, VerdictClass: emit.VerdictExecuted},
			Effect: &emit.Effect{Type: "test.tool.send", Status: emit.EffectConfirmed,
				IrreversibilityClass: emit.IrreversibilityOneWayConsequential, EffectAttestation: emit.AttestationGateExecuted,
				RequestDigest: requestDigest, ResponseDigest: responseDigest},
		},
		Payload: map[string]any{"synthetic": true}, Identity: identity,
	})
	require.NoError(t, err)
	record := artifact.Record{CapsuleID: sealed.CapsuleID, Capsule: sealed.Payload, ProducerEnvelope: sealed.Envelope,
		Artifacts: []artifact.Artifact{
			{Name: "request", Binding: artifact.EffectRequestDigest, Content: request, State: artifact.Present},
			{Name: "response", Binding: artifact.EffectResponseDigest, Content: response, State: artifact.Present},
		},
	}
	checks, err := artifact.Verify(record, []ed25519.PublicKey{pub})
	require.NoError(t, err)
	assert.True(t, checks["request"].Verified)
	assert.True(t, checks["response"].Verified)
	for i, a := range record.Artifacts {
		t.Run(a.Name, func(t *testing.T) {
			copy := record
			copy.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
			copy.Artifacts[i].Content = []byte(`{"different":true}`)
			_, err := artifact.Verify(copy, []ed25519.PublicKey{pub})
			require.ErrorIs(t, err, artifact.ErrDigestMismatch)
			missing, missingPub := testutil.Record(t, "missing-effect")
			missing.Artifacts = []artifact.Artifact{a}
			_, err = artifact.Verify(missing, []ed25519.PublicKey{missingPub})
			require.ErrorIs(t, err, artifact.ErrInvalid)
		})
	}
}
