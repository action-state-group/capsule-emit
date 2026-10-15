package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

const vectorsDir = "../../../../test-vectors/artifact-store"

// TestReproducesGoldenReports: the Go adapter's reports are the interlock's
// goldens, for each local format.
func TestReproducesGoldenReports(t *testing.T) {
	want := map[string][]byte{}
	for _, phase := range []string{"write", "read", "strict", "check"} {
		data, err := os.ReadFile(filepath.Join(vectorsDir, "interlock-"+phase+".txt"))
		require.NoError(t, err)
		want[phase] = data
	}
	for _, format := range []string{"jsonl", "sqlite"} {
		target := filepath.Join(t.TempDir(), "store."+format)
		for _, phase := range []string{"write", "read", "strict", "check"} {
			var out bytes.Buffer
			require.NoError(t, run([]string{phase, format, target, "interlock", vectorsDir}, &out))
			require.Equal(t, string(want[phase]), out.String(), "%s %s", format, phase)
		}
	}
}

func TestRejectsBadArguments(t *testing.T) {
	var out bytes.Buffer
	require.Error(t, run(nil, &out))
	require.Error(t, run([]string{"write", "csv", "x", "ns", vectorsDir}, &out))
	require.Error(t, run([]string{"scan", "jsonl", filepath.Join(t.TempDir(), "a"), "ns", vectorsDir}, &out))
	require.Error(t, run([]string{"write", "jsonl", "x", "ns", t.TempDir()}, &out))
	require.Error(t, run([]string{"write", "jsonl", "x", "-bad", vectorsDir}, &out))
	require.Equal(t, "storage_error", outcome(os.ErrNotExist))
}
