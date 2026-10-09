package jsonl_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	jsonlstore "github.com/action-state-group/capsule-emit-go/artifact/jsonl"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newStore(t *testing.T, namespace string, trusted []ed25519.PublicKey) (*jsonlstore.Store, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")
	s, err := jsonlstore.New(path, namespace, trusted)
	require.NoError(t, err)
	require.NoError(t, s.Init(t.Context()))
	return s, path
}

func TestJSONLLifecycle(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-lifecycle")
	store, _ := newStore(t, "test-lifecycle", []ed25519.PublicKey{pub})

	// Init is idempotent.
	require.NoError(t, store.Init(t.Context()))

	// Put is idempotent for byte-identical retries.
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.Put(t.Context(), record))

	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
	assert.Equal(t, record.ProducerEnvelope, got.ProducerEnvelope)

	byName := map[string]artifact.Artifact{}
	for _, a := range got.Artifacts {
		byName[a.Name] = a
	}
	assert.Equal(t, record.Artifacts[0].Content, byName["payload"].Content)
	assert.Equal(t, record.Artifacts[1].Content, byName["agent_output"].Content)
}

func TestJSONLConflictOnChangedBytes(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-conflict")
	store, _ := newStore(t, "test-conflict", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))

	conflict := record
	conflict.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
	conflict.Artifacts[2].Content = []byte("changed unbound attachment")
	err := store.Put(t.Context(), conflict)
	assert.ErrorIs(t, err, artifact.ErrConflict)
}

func TestJSONLGetNotFound(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-missing")
	store, _ := newStore(t, "test-missing", []ed25519.PublicKey{pub})
	_, err := store.Get(t.Context(), fmt.Sprintf("%064x", 0))
	assert.ErrorIs(t, err, artifact.ErrNotFound)
}

func TestJSONLPurgeMarksOriginalsAndBlocksResurrection(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-purge")
	store, _ := newStore(t, "test-purge", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))

	// Purge is idempotent.
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))

	// Get still succeeds; bound originals are tombstoned, not removed from inventory.
	got, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	for _, a := range got.Artifacts {
		if a.Binding != "" {
			assert.Equal(t, artifact.Purged, a.State)
			assert.NotEmpty(t, a.ContentSHA256, "tombstone must keep content_sha256")
			assert.Nil(t, a.Content, "tombstone must have no content")
		}
	}

	// A retry after purge cannot resurrect originals.
	require.ErrorIs(t, store.Put(t.Context(), record), artifact.ErrPurged)
}

func TestJSONLPurgeIdempotent(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-purge-idem")
	store, _ := newStore(t, "test-purge-idem", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))
}

func TestJSONLGetRejectsMalformedID(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-badid")
	store, _ := newStore(t, "test-badid", []ed25519.PublicKey{pub})
	_, err := store.Get(t.Context(), "not-a-hex-id")
	assert.ErrorIs(t, err, artifact.ErrInvalid)
}

func TestJSONLNewRejectsInvalidConfig(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-new")
	good := []ed25519.PublicKey{pub}
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")
	cases := map[string]struct {
		path      string
		namespace string
		trusted   []ed25519.PublicKey
	}{
		"empty namespace": {path, "", good},
		"bad namespace":   {path, "bad namespace!", good},
		"no trusted keys": {path, "ns", nil},
		"bad key size":    {path, "ns", []ed25519.PublicKey{{1, 2, 3}}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := jsonlstore.New(tc.path, tc.namespace, tc.trusted)
			assert.ErrorIs(t, err, artifact.ErrInvalid)
		})
	}
}

func TestJSONLNamespace(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-ns")
	store, _ := newStore(t, "test-namespace", []ed25519.PublicKey{pub})
	assert.Equal(t, "test-namespace", store.Namespace())
}

func TestJSONLPurgeRejectsMissingAndMalformed(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-purge-missing")
	store, _ := newStore(t, "test-purge-missing", []ed25519.PublicKey{pub})
	assert.ErrorIs(t, store.Purge(t.Context(), fmt.Sprintf("%064x", 0)), artifact.ErrNotFound)
	assert.ErrorIs(t, store.Purge(t.Context(), "not-a-hex-id"), artifact.ErrInvalid)
}

func TestJSONLUntrustedSignerRejectedOnPut(t *testing.T) {
	record, _ := testutil.Record(t, "jsonl-untrusted-put")
	// Store trusts a different key than the record's signer.
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	store, _ := newStore(t, "test-untrusted-put", []ed25519.PublicKey{otherPub})
	assert.ErrorIs(t, store.Put(t.Context(), record), artifact.ErrUntrustedSigner)
}

func TestJSONLUntrustedSignerRejectedOnGet(t *testing.T) {
	record, pub1 := testutil.Record(t, "jsonl-untrusted-get")
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")

	// Write the record into a store that trusts the record's own signer.
	store1, err := jsonlstore.New(path, "test-untrusted-get", []ed25519.PublicKey{pub1})
	require.NoError(t, err)
	require.NoError(t, store1.Init(t.Context()))
	require.NoError(t, store1.Put(t.Context(), record))

	// Re-open with a different trusted key; Get must reject the stored record.
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	store2, err := jsonlstore.New(path, "test-untrusted-get", []ed25519.PublicKey{otherPub})
	require.NoError(t, err)
	_, err = store2.Get(t.Context(), record.CapsuleID)
	assert.ErrorIs(t, err, artifact.ErrUntrustedSigner)
}

func TestJSONLCorruptLine(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-corrupt")
	store, path := newStore(t, "test-corrupt", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))

	// Corrupt the file content in the middle of the stored JSON, leaving the
	// first few bytes (including the capsule_id field) intact so the index still
	// has an entry and Get can reach the line.
	data, err := os.ReadFile(path)
	require.NoError(t, err)
	// Overwrite some bytes near the end of the first line with garbage.
	// The line ends with '\n'; smash bytes just before it.
	nl := len(data) - 1
	for i := nl - 20; i < nl-5; i++ {
		data[i] = 0xff
	}
	require.NoError(t, os.WriteFile(path, data, 0o644))

	// The in-memory index still has the entry; Get must now return ErrCorrupt.
	_, err = store.Get(t.Context(), record.CapsuleID)
	assert.ErrorIs(t, err, artifact.ErrCorrupt)
}

func TestJSONLPurgePreservesStorageChecksum(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-checksum")
	store, _ := newStore(t, "test-checksum", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))

	// Capture checksum before purge.
	before, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	beforeSum, err := before.StorageChecksum()
	require.NoError(t, err)

	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))

	after, err := store.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	afterSum, err := after.StorageChecksum()
	require.NoError(t, err)

	assert.Equal(t, beforeSum, afterSum, "StorageChecksum must be invariant across purge")
}

func TestJSONLReopenRebuildIndex(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-reopen")
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")

	store1, err := jsonlstore.New(path, "test-reopen", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store1.Init(t.Context()))
	require.NoError(t, store1.Put(t.Context(), record))

	// Open a fresh store from the same file; it must rebuild its index.
	store2, err := jsonlstore.New(path, "test-reopen", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	got, err := store2.Get(t.Context(), record.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, record.Capsule, got.Capsule)
}

func TestJSONLPurgePreservesFileMode(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-mode")
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")

	store, err := jsonlstore.New(path, "test-mode", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.NoError(t, store.Init(t.Context()))
	require.NoError(t, os.Chmod(path, 0o640))
	require.NoError(t, store.Put(t.Context(), record))

	require.NoError(t, store.Purge(t.Context(), record.CapsuleID))

	info, err := os.Stat(path)
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0o640), info.Mode().Perm(), "purge rewrite must preserve file mode")
}
