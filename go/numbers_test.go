package emit

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"strconv"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func decodeUseNumber(t *testing.T, text string) any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(text)))
	dec.UseNumber()
	var v any
	require.NoError(t, dec.Decode(&v))
	return v
}

func TestCanonicalizeForDigestSharedVectors(t *testing.T) {
	raw, err := os.ReadFile("../test-vectors/number-canonicalization/vectors.json")
	require.NoError(t, err)
	var doc struct {
		Cases []struct {
			Name      string `json:"name"`
			InputJSON string `json:"input_json"`
			Digest    string `json:"digest"`
			Error     bool   `json:"error"`
		} `json:"cases"`
	}
	require.NoError(t, json.Unmarshal(raw, &doc))
	require.NotEmpty(t, doc.Cases)
	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			// A number that overflows binary64 may fail at decode time in some
			// decoders; json.Decoder accepts the token, so decode never fails.
			canon, err := CanonicalizeForDigest(decodeUseNumber(t, c.InputJSON))
			if err == nil {
				var digest string
				digest, err = DigestJSON(canon)
				if !c.Error {
					require.NoError(t, err)
					assert.Equal(t, c.Digest, digest)
					return
				}
			}
			assert.True(t, c.Error, "unexpected error: %v", err)
			assert.Error(t, err)
		})
	}
}

func TestCanonicalizeForDigestMakesFloatArgumentsDigestible(t *testing.T) {
	input := decodeUseNumber(t, `{"temperature":0.5}`)
	_, err := DigestJSON(input)
	require.Error(t, err, "DigestJSON stays strict")
	canon, err := CanonicalizeForDigest(input)
	require.NoError(t, err)
	assert.Equal(t, map[string]any{"temperature": "0.5"}, canon)
	_, err = DigestJSON(canon)
	require.NoError(t, err)
}

func TestCanonicalizeForDigestGoNumberTypes(t *testing.T) {
	canon, err := CanonicalizeForDigest(map[string]any{
		"f64":  0.75,
		"f32":  float32(0.5),
		"negz": math.Copysign(0, -1),
		"i":    int(7),
		"i64":  int64(-7),
		"u64":  uint64(7),
		"u8":   uint8(1),
		"list": []any{1.5, json.Number("2.0"), json.Number("-0"), "s", true, nil},
		"raw":  struct{ A int }{1},
	})
	require.NoError(t, err)
	assert.Equal(t, map[string]any{
		"f64":  "0.75",
		"f32":  "0.5",
		"negz": "0",
		"i":    int64(7),
		"i64":  int64(-7),
		"u64":  uint64(7),
		"u8":   uint8(1),
		"list": []any{"1.5", "2", json.Number("0"), "s", true, nil},
		"raw":  struct{ A int }{1},
	}, canon)
}

func TestCanonicalizeForDigestDoesNotMutateInput(t *testing.T) {
	input := map[string]any{"a": []any{0.5}}
	_, err := CanonicalizeForDigest(input)
	require.NoError(t, err)
	assert.Equal(t, map[string]any{"a": []any{0.5}}, input)
}

func TestCanonicalizeForDigestErrorsNameTheField(t *testing.T) {
	for name, tc := range map[string]struct {
		value any
		field string
	}{
		"nan":              {map[string]any{"a": []any{math.NaN()}}, `a[0]`},
		"inf":              {map[string]any{"a": map[string]any{"b": math.Inf(-1)}}, `a.b`},
		"overflow":         {map[string]any{"x": json.Number("1e999")}, `x`},
		"unsafe int token": {map[string]any{"x": json.Number("9007199254740992")}, `x`},
		"huge int token":   {map[string]any{"x": json.Number("123456789012345678901234567890")}, `x`},
		"unsafe int64":     {map[string]any{"x": int64(1 << 60)}, `x`},
		"unsafe uint64":    {map[string]any{"x": uint64(1 << 60)}, `x`},
		"malformed":        {map[string]any{"x": json.Number("1.2.3")}, `x`},
		"leading plus":     {map[string]any{"x": json.Number("+1")}, `x`},
		"leading zero":     {map[string]any{"x": json.Number("01")}, `x`},
		"bare fraction":    {map[string]any{"x": json.Number(".5")}, `x`},
		"trailing dot":     {map[string]any{"x": json.Number("1.")}, `x`},
		"hex float":        {map[string]any{"x": json.Number("0x1.8p1")}, `x`},
		"word":             {map[string]any{"x": json.Number("NaN")}, `x`},
		"empty":            {map[string]any{"x": json.Number("")}, `x`},
		"slice element":    {[]any{json.Number("1e999")}, `[0]`},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := CanonicalizeForDigest(tc.value)
			require.Error(t, err)
			assert.Contains(t, err.Error(), tc.field)
		})
	}
	_, err := CanonicalizeForDigest(math.NaN())
	require.Error(t, err)
	assert.NotContains(t, err.Error(), "field:")
}

func TestCanonicalizeForDigestNativeIntegerWidths(t *testing.T) {
	big := int64(1) << 60 // runtime values: constant conversion would not compile on 32-bit
	if strconv.IntSize == 64 {
		_, err := CanonicalizeForDigest(int(big))
		require.Error(t, err)
		_, err = CanonicalizeForDigest(uint(big))
		require.Error(t, err)
	}
	got, err := CanonicalizeForDigest(uint(5))
	require.NoError(t, err)
	assert.Equal(t, uint64(5), got)
}

func TestFloat64ToJCS(t *testing.T) {
	for in, want := range map[float64]string{
		1:                     "1",
		-1.5:                  "-1.5",
		123456789012345680000: "123456789012345680000",
		1e21:                  "1e+21",
		1.5e21:                "1.5e+21",
		0.000001:              "0.000001",
		1e-7:                  "1e-7",
		1.25e-7:               "1.25e-7",
		5e-324:                "5e-324",
		math.MaxFloat64:       "1.7976931348623157e+308",
		123.456:               "123.456",
		0.1:                   "0.1",
		4.35:                  "4.35",
	} {
		got, err := float64ToJCS(in, "")
		require.NoError(t, err)
		assert.Equal(t, want, got)
	}
}
