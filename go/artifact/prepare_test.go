package artifact_test

import (
	"bytes"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestPrepareOwnershipAndInventory(t *testing.T) {
	raw, _ := testutil.Record(t, "ownership")
	prepared, err := artifact.Prepare(raw)
	require.NoError(t, err)
	assert.Len(t, prepared.Artifacts[0].ContentSHA256, 64)
	assert.Empty(t, raw.Artifacts[0].ContentSHA256)
	checksum, err := prepared.StorageChecksum()
	require.NoError(t, err)
	prepared.Artifacts[0], prepared.Artifacts[1] = prepared.Artifacts[1], prepared.Artifacts[0]
	reordered, err := prepared.StorageChecksum()
	require.NoError(t, err)
	assert.Equal(t, checksum, reordered)
	prepared.Artifacts[0].State = artifact.Purged
	prepared.Artifacts[0].Content = nil
	purged, err := prepared.StorageChecksum()
	require.NoError(t, err)
	assert.Equal(t, checksum, purged)
	prepared.Artifacts = prepared.Artifacts[:2]
	missing, err := prepared.StorageChecksum()
	require.NoError(t, err)
	assert.NotEqual(t, checksum, missing)
	prepared.Capsule[0] = '!'
	prepared.ProducerEnvelope[0] ^= 1
	assert.NotEqual(t, raw.Capsule, prepared.Capsule)
	assert.NotEqual(t, raw.ProducerEnvelope, prepared.ProducerEnvelope)
}

func TestPrepareLimits(t *testing.T) {
	for _, test := range []struct {
		name   string
		record artifact.Record
		want   error
	}{
		{"artifact_limit", artifact.Record{Artifacts: make([]artifact.Artifact, 65)}, artifact.ErrInvalid},
		{"envelope_limit", artifact.Record{ProducerEnvelope: make([]byte, 65536)}, artifact.ErrInvalid},
		{"byte_limit", artifact.Record{Capsule: make([]byte, 8*1024*1024+1)}, artifact.ErrInvalid},
		{"purged", artifact.Record{Artifacts: []artifact.Artifact{{State: artifact.Purged}}}, artifact.ErrPurged},
		{"checksum", artifact.Record{Artifacts: []artifact.Artifact{{State: artifact.Present, Content: []byte("original"), ContentSHA256: "wrong"}}}, artifact.ErrCorrupt},
	} {
		t.Run(test.name, func(t *testing.T) { _, err := artifact.Prepare(test.record); require.ErrorIs(t, err, test.want) })
	}
	raw := artifact.Record{Artifacts: []artifact.Artifact{{State: artifact.Present, Content: []byte{}}}}
	prepared, err := artifact.Prepare(raw)
	require.NoError(t, err)
	require.NotNil(t, prepared.Artifacts[0].Content)
	assert.True(t, bytes.Equal([]byte{}, prepared.Artifacts[0].Content))
}
