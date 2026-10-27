package index

import (
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseTimestamp(t *testing.T) {
	for input, want := range map[string]int64{
		"1970-01-01T00:00:00Z":             0,
		"1970-01-01T00:00:00.000001Z":      1,
		"1970-01-01T00:00:00.0000019Z":     1,
		"1970-01-01T01:00:00+01:00":        0,
		"1969-12-31T23:00:00-01:00":        0,
		"2024-02-29T00:00:00Z":             1709164800_000000,
		"0000-01-01T00:00:00Z":             -62167219200_000000,
		"9999-12-31T23:59:59.999999+00:00": 253402300799_999999,
	} {
		got, ok := ParseTimestamp(input)
		require.True(t, ok, input)
		assert.Equal(t, want, got, input)
	}
	for _, input := range []string{"", "2023-02-29T00:00:00Z", "2026-13-01T00:00:00Z", "2026-00-01T00:00:00Z",
		"2026-01-00T00:00:00Z", "2026-01-01T00:60:00Z", "2026-01-01T00:00:00+00:60", "2026-01-01T00:00:00"} {
		_, ok := ParseTimestamp(input)
		assert.False(t, ok, input)
	}
}

func TestExtract(t *testing.T) {
	got, err := Extract([]byte(`{"a":{"b":"x","c":["y",1,"y","z"],"d":{"e":1}},"timestamp":"bad"}`),
		[]string{"a.b", "a.c", "a.d", "a.b.x", "missing"})
	require.NoError(t, err)
	assert.False(t, got.HasTS)
	assert.Equal(t, []Entry{{"a.b", "x"}, {"a.c", "y"}, {"a.c", "z"}}, got.Entries)

	got, err = Extract([]byte(`["not an object"]`), []string{"a"})
	require.NoError(t, err)
	assert.Empty(t, got.Entries)

	_, err = Extract([]byte(`{`), nil)
	assert.ErrorIs(t, err, artifact.ErrCorrupt)
}

func TestPathsRoundTrip(t *testing.T) {
	paths, err := NormalizePaths([]string{"b", "a.c"})
	require.NoError(t, err)
	assert.Equal(t, `["a.c","b"]`, EncodePaths(paths))
	decoded, err := DecodePaths(EncodePaths(paths))
	require.NoError(t, err)
	assert.Equal(t, paths, decoded)
	none, err := NormalizePaths(nil)
	require.NoError(t, err)
	assert.Equal(t, `[]`, EncodePaths(none))

	for _, stored := range []string{`{`, `null`, `["a","a"]`, `[" "]`} {
		_, err := DecodePaths(stored)
		assert.ErrorIs(t, err, artifact.ErrCorrupt, stored)
	}
}

func TestMemoryBeforeDeclaration(t *testing.T) {
	var m Memory
	require.NoError(t, m.Add("id", []byte(`{`)), "Add ignores records before a declaration")
	_, ok := m.Declared()
	assert.False(t, ok)
	_, err := m.Declare([]string{"a", "a"})
	assert.ErrorIs(t, err, artifact.ErrInvalid)
	fresh, err := m.Declare([]string{"a"})
	require.NoError(t, err)
	assert.True(t, fresh)
	assert.ErrorIs(t, m.Add("id", []byte(`{`)), artifact.ErrCorrupt)
	paths, ok := m.Declared()
	assert.True(t, ok)
	assert.Equal(t, []string{"a"}, paths)
}

func TestValueKey(t *testing.T) {
	assert.Equal(t, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", ValueKey(""))
}
