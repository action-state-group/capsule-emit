package artifact_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestVerify(t *testing.T) {
	record, pub := testutil.Record(t, "verify")
	trusted := []ed25519.PublicKey{pub}
	checks, err := artifact.Verify(record, trusted)
	require.NoError(t, err)
	assert.True(t, checks["payload"].Verified)
	assert.True(t, checks["agent_output"].Verified)
	assert.False(t, checks["private_note"].Bound)
	assert.False(t, checks["private_note"].Verified)

	for _, test := range []struct {
		name   string
		mutate func(*artifact.Record)
		want   error
	}{
		{"wrong_id", func(r *artifact.Record) { r.CapsuleID = "bad" }, artifact.ErrInvalid},
		{"wrong_original", func(r *artifact.Record) { r.Artifacts[0].Content = []byte(`{"wrong":true}`) }, artifact.ErrDigestMismatch},
		{"duplicate_json_key", func(r *artifact.Record) { r.Artifacts[0].Content = []byte(`{"n":1,"n":2}`) }, artifact.ErrDigestMismatch},
		{"unknown_binding", func(r *artifact.Record) { r.Artifacts[0].Binding = "input_digest" }, artifact.ErrInvalid},
		{"duplicate_name", func(r *artifact.Record) { r.Artifacts[1].Name = "payload" }, artifact.ErrInvalid},
		{"missing_bytes", func(r *artifact.Record) { r.Artifacts[0].Content = nil }, artifact.ErrInvalid},
		{"invalid_state", func(r *artifact.Record) { r.Artifacts[0].State = "verified" }, artifact.ErrInvalid},
		{"false_never_retained", func(r *artifact.Record) { r.Artifacts[0].State = artifact.NeverRetained }, artifact.ErrInvalid},
		{"bad_tombstone", func(r *artifact.Record) { r.Artifacts[0].State = artifact.Purged; r.Artifacts[0].Content = nil }, artifact.ErrInvalid},
	} {
		t.Run(test.name, func(t *testing.T) {
			copy := record
			copy.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
			test.mutate(&copy)
			_, err := artifact.Verify(copy, trusted)
			require.ErrorIs(t, err, test.want)
		})
	}
	_, err = artifact.Verify(record, nil)
	require.ErrorIs(t, err, artifact.ErrUntrustedSigner)
	other, _, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	_, err = artifact.Verify(record, []ed25519.PublicKey{other})
	require.ErrorIs(t, err, artifact.ErrUntrustedSigner)
	record.Artifacts[1].Content = nil
	record.Artifacts[1].State = artifact.NeverRetained
	checks, err = artifact.Verify(record, trusted)
	require.NoError(t, err)
	assert.Equal(t, artifact.NeverRetained, checks["agent_output"].State)
	assert.False(t, checks["agent_output"].Verified)
}
