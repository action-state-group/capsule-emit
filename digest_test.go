package emit

import (
	"encoding/json"
	"encoding/json/jsontext"
	"errors"
	"math"
	"testing"
	"time"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type taggedDigestInput struct {
	Message string `json:"message"`
	Count   int    `json:"count"`
}

type rawJSONMarshaler string

func (value rawJSONMarshaler) MarshalJSON() ([]byte, error) { return []byte(value), nil }

type failingDigestJSON struct{}

func (failingDigestJSON) MarshalJSON() ([]byte, error) { return nil, errors.New("marshal failed") }

func TestDigestJSONMarshalsTypedValuesAndCanonicalizes(t *testing.T) {
	value := taggedDigestInput{Message: "sent", Count: 2}
	digest, err := DigestJSON(value)
	require.NoError(t, err)

	expected, err := canonical.JSONDigest(map[string]any{
		"count": json.Number("2"), "message": "sent",
	})
	require.NoError(t, err)
	assert.Equal(t, expected, digest)

	reordered, err := DigestJSON(map[string]any{"message": "sent", "count": 2})
	require.NoError(t, err)
	assert.Equal(t, digest, reordered)

	custom, err := DigestJSON(rawJSONMarshaler(`{"ok":true}`))
	require.NoError(t, err)
	expected, err = canonical.JSONDigest(map[string]any{"ok": true})
	require.NoError(t, err)
	assert.Equal(t, expected, custom)
}

func TestDigestJSONRejectsUnsafeJSON(t *testing.T) {
	for _, test := range []struct {
		name     string
		value    any
		contains string
	}{
		{name: "marshal", value: failingDigestJSON{}, contains: "marshal JSON digest input"},
		{name: "invalid UTF-8 value", value: map[string]any{"value": string([]byte{0xff})}, contains: "invalid UTF-8"},
		{name: "invalid UTF-8 key", value: map[string]any{string([]byte{0xff}): true}, contains: "invalid UTF-8"},
		{name: "lone surrogate", value: rawJSONMarshaler(`{"value":"\ud800"}`), contains: "invalid surrogate pair"},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := DigestJSON(test.value)
			assert.ErrorContains(t, err, test.contains)
		})
	}

	_, err := DigestJSON(rawJSONMarshaler(`{"a":1,"a":2}`))
	var syntacticError *jsontext.SyntacticError
	assert.ErrorAs(t, err, &syntacticError)
	assert.ErrorContains(t, err, "duplicate object member name")

	_, err = DigestJSON(map[string]any{"value": 1.5})
	var floatError *canonical.FloatError
	assert.ErrorAs(t, err, &floatError)

	_, err = DigestJSON(map[string]any{"value": json.Number("9007199254740992")})
	var unsafeIntError *canonical.UnsafeIntError
	assert.ErrorAs(t, err, &unsafeIntError)
}

func TestDigestJSONCanonicalizesNegativeZero(t *testing.T) {
	topLevelNegativeZero, err := DigestJSON(math.Copysign(0, -1))
	require.NoError(t, err)
	topLevelPositiveZero, err := DigestJSON(0)
	require.NoError(t, err)
	assert.Equal(t, topLevelPositiveZero, topLevelNegativeZero)

	negativeZero, err := DigestJSON(map[string]any{"value": math.Copysign(0, -1)})
	require.NoError(t, err)
	encodedNegativeZero, err := DigestJSON(rawJSONMarshaler(`{"value":-0}`))
	require.NoError(t, err)
	positiveZero, err := DigestJSON(map[string]any{"value": 0})
	require.NoError(t, err)
	assert.Equal(t, positiveZero, negativeZero)
	assert.Equal(t, positiveZero, encodedNegativeZero)

	arrayNegativeZero, err := DigestJSON([]any{math.Copysign(0, -1)})
	require.NoError(t, err)
	arrayPositiveZero, err := DigestJSON([]any{0})
	require.NoError(t, err)
	assert.Equal(t, arrayPositiveZero, arrayNegativeZero)
}

func TestDigestJSONEnforcesMaximumDepth(t *testing.T) {
	var value any = "leaf"
	for range maxJSONDepth {
		value = []any{value}
	}
	_, err := DigestJSON(value)
	assert.ErrorContains(t, err, "maximum depth")
}

func TestDigestJSONAcceptsJSONNull(t *testing.T) {
	digest, err := DigestJSON(nil)
	require.NoError(t, err)
	expected, err := canonical.JSONDigest(nil)
	require.NoError(t, err)
	assert.Equal(t, expected, digest)
}

func TestDigestJSONAllowsReplacementCharacter(t *testing.T) {
	_, err := DigestJSON(map[string]any{"value": "�"})
	require.NoError(t, err)
}

func TestDigestJSONUsesEncodingJSONV2Semantics(t *testing.T) {
	digest, err := DigestJSON(map[string]any{
		"map":   map[string]string(nil),
		"slice": []string(nil),
	})
	require.NoError(t, err)

	expected, err := canonical.JSONDigest(map[string]any{
		"map":   map[string]any{},
		"slice": []any{},
	})
	require.NoError(t, err)
	assert.Equal(t, expected, digest)

	_, err = DigestJSON(5 * time.Second)
	assert.ErrorContains(t, err, "time.Duration")
}
