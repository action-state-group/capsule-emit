package jsonl_test

import (
	"crypto/ed25519"
	"os"
	"path/filepath"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/action-state-group/capsule-emit-go/artifact/internal/testutil"
	jsonlstore "github.com/action-state-group/capsule-emit-go/artifact/jsonl"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestJSONLPutRequiresInitializedFile: Put on a store whose file was never
// created (Init not called) fails instead of silently creating it. Init is the
// explicit provisioning step, matching the sqlite/mysql backends.
func TestJSONLPutRequiresInitializedFile(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-noinit")
	path := filepath.Join(t.TempDir(), "artifacts.jsonl")
	s, err := jsonlstore.New(path, "test-noinit", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.Error(t, s.Put(t.Context(), record))
}

// TestJSONLPutRejectsRecordOverLimit: Put enforces the v1 admission limits via
// Prepare; a record with more than 64 artifacts is rejected as invalid.
func TestJSONLPutRejectsRecordOverLimit(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-limit")
	store, _ := newStore(t, "test-limit", []ed25519.PublicKey{pub})
	// A valid record padded past the 64-artifact v1 limit: removing the limit
	// would let this Put succeed, so ErrInvalid specifically proves the limit.
	oversized := record
	oversized.Artifacts = append([]artifact.Artifact(nil), record.Artifacts...)
	for len(oversized.Artifacts) <= 64 {
		oversized.Artifacts = append(oversized.Artifacts, artifact.Artifact{Name: "pad", Content: []byte("x"), State: artifact.Present})
	}
	require.ErrorIs(t, store.Put(t.Context(), oversized), artifact.ErrInvalid)
}

// TestJSONLOperationsAfterFileRemoved: if the backing file disappears after the
// index was built, Get, a conflicting Put re-read, and Purge all surface the
// read error rather than panicking or returning a silent/empty result.
func TestJSONLOperationsAfterFileRemoved(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-removed")
	store, path := newStore(t, "test-removed", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))
	require.NoError(t, os.Remove(path))

	_, getErr := store.Get(t.Context(), record.CapsuleID)
	require.Error(t, getErr)
	require.Error(t, store.Put(t.Context(), record))
	require.Error(t, store.Purge(t.Context(), record.CapsuleID))
}

// TestJSONLPurgeReadOnlyDirectory: Purge rewrites through a temp file in the
// store's directory, so a read-only directory makes it fail instead of losing
// or corrupting data.
func TestJSONLPurgeReadOnlyDirectory(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses directory permissions")
	}
	record, pub := testutil.Record(t, "jsonl-rodir")
	store, path := newStore(t, "test-rodir", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))
	dir := filepath.Dir(path)
	require.NoError(t, os.Chmod(dir, 0o500))
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	require.Error(t, store.Purge(t.Context(), record.CapsuleID))
}

// TestJSONLGetTruncatedFile: a read whose indexed line is truncated on disk
// fails closed with ErrCorrupt instead of returning a partial record.
func TestJSONLGetTruncatedFile(t *testing.T) {
	record, pub := testutil.Record(t, "jsonl-truncated")
	store, path := newStore(t, "test-truncated", []ed25519.PublicKey{pub})
	require.NoError(t, store.Put(t.Context(), record))
	// Truncate to fewer bytes than the indexed line length so ReadAt short-reads.
	require.NoError(t, os.Truncate(path, 8))
	_, err := store.Get(t.Context(), record.CapsuleID)
	require.ErrorIs(t, err, artifact.ErrCorrupt)
}

// TestJSONLPurgePreservesOtherRecords: purging one record rewrites the file,
// copying every other record's line verbatim and rebuilding the index, so a
// sibling record still reads and verifies while the target is tombstoned.
func TestJSONLPurgePreservesOtherRecords(t *testing.T) {
	recA, pubA := testutil.Record(t, "jsonl-multi-a")
	recB, pubB := testutil.Record(t, "jsonl-multi-b")
	store, _ := newStore(t, "test-multi", []ed25519.PublicKey{pubA, pubB})
	require.NoError(t, store.Put(t.Context(), recA))
	require.NoError(t, store.Put(t.Context(), recB))

	require.NoError(t, store.Purge(t.Context(), recA.CapsuleID))

	gotB, err := store.Get(t.Context(), recB.CapsuleID)
	require.NoError(t, err)
	assert.Equal(t, recB.Capsule, gotB.Capsule)
	assert.Equal(t, recB.ProducerEnvelope, gotB.ProducerEnvelope)
	require.Len(t, gotB.Artifacts, len(recB.Artifacts))
	gotByName := map[string]artifact.Artifact{}
	for _, a := range gotB.Artifacts {
		gotByName[a.Name] = a
	}
	for _, a := range recB.Artifacts {
		assert.Equal(t, a.Content, gotByName[a.Name].Content, "sibling artifact content untouched")
		assert.Equal(t, a.State, gotByName[a.Name].State, "sibling artifact not purged")
	}

	gotA, err := store.Get(t.Context(), recA.CapsuleID)
	require.NoError(t, err)
	for _, a := range gotA.Artifacts {
		assert.NotEqual(t, artifact.Present, a.State, "purged record keeps no present originals")
	}
}

// TestJSONLNewRejectsUnreadablePath: New over a path that cannot be scanned as a
// JSONL file (here a directory) surfaces the scan error rather than pretending
// the store is empty.
func TestJSONLNewRejectsUnreadablePath(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-badpath")
	_, err := jsonlstore.New(t.TempDir(), "test-badpath", []ed25519.PublicKey{pub})
	require.Error(t, err)
}

// TestJSONLInitRejectsUncreatablePath: Init fails when the file cannot be created
// (its parent directory does not exist) rather than silently succeeding.
func TestJSONLInitRejectsUncreatablePath(t *testing.T) {
	_, pub := testutil.Record(t, "jsonl-initbad")
	path := filepath.Join(t.TempDir(), "missing-parent", "artifacts.jsonl")
	s, err := jsonlstore.New(path, "test-initbad", []ed25519.PublicKey{pub})
	require.NoError(t, err)
	require.Error(t, s.Init(t.Context()))
}
