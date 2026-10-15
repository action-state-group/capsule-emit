package artifacttest_test

import (
	"crypto/ed25519"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/action-state-group/capsule-emit/go/artifact/artifacttest"
	"github.com/action-state-group/capsule-emit/go/artifact/memory"
	"github.com/stretchr/testify/require"
)

// TestChecksumVectors pins Record.StorageChecksum, including the two goldens
// the TypeScript suite also pins (ac78a7aa... and 33be5d36...).
func TestChecksumVectors(t *testing.T) {
	v, err := artifacttest.Load()
	require.NoError(t, err)
	require.Equal(t, "artifact-store-conformance/v1", v.Format)
	got := map[string]string{}
	for _, c := range v.Checksums {
		sum, err := v.Records[c.Record].StorageChecksum()
		require.NoError(t, err)
		require.Equal(t, c.StorageChecksum, sum, c.Record)
		got[c.Record] = sum
	}
	require.Equal(t, "ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9", got["synthetic.stored"])
	require.Equal(t, "33be5d36fbe3fcd89e5ec0a3ac5feb0ea764fa08053c34d0978c99fc2dfb71f4", got["synthetic.empty"])
	require.Equal(t, got["basic.stored"], got["basic.purged"], "purge must not change the storage checksum")
}

// TestDiff covers each difference Diff reports, including absent versus
// zero-length content.
func TestDiff(t *testing.T) {
	v, err := artifacttest.Load()
	require.NoError(t, err)
	stored := v.Records["basic.stored"]
	require.Empty(t, artifacttest.Diff(stored, stored))
	mutate := func(change func(*artifact.Record)) artifact.Record {
		r := stored
		r.Artifacts = append([]artifact.Artifact{}, stored.Artifacts...)
		change(&r)
		return r
	}
	for name, got := range map[string]artifact.Record{
		"id":       mutate(func(r *artifact.Record) { r.CapsuleID = "b" }),
		"capsule":  mutate(func(r *artifact.Record) { r.Capsule = []byte("{}") }),
		"envelope": mutate(func(r *artifact.Record) { r.ProducerEnvelope = nil }),
		"count":    mutate(func(r *artifact.Record) { r.Artifacts = r.Artifacts[:1] }),
		"missing":  mutate(func(r *artifact.Record) { r.Artifacts[0].Name = "other" }),
		"binding":  mutate(func(r *artifact.Record) { r.Artifacts[0].Binding = "" }),
		"state":    v.Records["basic.purged"],
		"sha":      mutate(func(r *artifact.Record) { r.Artifacts[0].ContentSHA256 = "" }),
		"presence": mutate(func(r *artifact.Record) { r.Artifacts[0].Content = nil; r.Artifacts[0].State = artifact.Present }),
		"content":  mutate(func(r *artifact.Record) { r.Artifacts[0].Content = []byte("x") }),
	} {
		require.NotEmpty(t, artifacttest.Diff(stored, got), name)
	}
	blank := v.Records["blank.stored"]
	absent := blank
	absent.Artifacts = []artifact.Artifact{blank.Artifacts[0]}
	absent.Artifacts[0].Content = nil
	require.NotEmpty(t, artifacttest.Diff(blank, absent), "absent content must differ from zero-length content")
}

// TestRunAgainstMemory exercises the runner itself on the reference in-memory
// backend; every other backend runs it from its own package.
func TestRunAgainstMemory(t *testing.T) {
	artifacttest.Run(t, artifacttest.Harness[struct{}]{
		NewBacking: func(*testing.T) struct{} { return struct{}{} },
		Open: func(_ *testing.T, _ struct{}, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error) {
			return memory.New(namespace, trusted)
		},
	})
}
