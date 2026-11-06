package emit

import (
	"encoding/json"
	"encoding/json/jsontext"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

// maxSafeInteger is the largest integer token DigestJSON accepts (2^53 - 1).
const maxSafeInteger = 1<<53 - 1

// CanonicalizeForDigest returns value with every non-integer number (a fraction
// or exponent token, a float64 or a float32) replaced by its RFC 8785 section
// 3.2.2.3 decimal string, so the result is accepted by
// DigestJSON. It is the Go twin of Python's
// capsule_emit.numbers.canonicalize_for_digest: the same input gives the same
// digest in both (see test-vectors/number-canonicalization).
//
// Use it at an adapter boundary, where a framework hands you whatever it
// decoded from a tool schema (an MCP argument such as {"temperature":0.5})
// and you cannot ask it for decimal strings. Do not use it when you own the
// data: DigestJSON stays strict and tells you about a float.
//
// The input is decoded JSON: map[string]any, []any, string, bool, nil,
// json.Number (decode with Decoder.UseNumber), float64 and float32, and the
// Go integer types. The conversion is deterministic:
//
//   - A json.Number with a fraction or exponent, a float64 or a float32
//     (widened to binary64 first, so float32(0.1) is "0.10000000149011612")
//     becomes a string: 0.5 -> "0.5", 1.0 -> "1", 1e21 -> "1e+21",
//     -0.0 -> "0" (RFC 8785 Appendix B).
//   - An integer token (json.Number without a fraction or exponent, or a Go
//     integer) stays an integer, so existing integer-argument digests do not
//     move. Consequently 1 and 1.0 digest differently ({"v":1} versus
//     {"v":"1"}), exactly as in Python. "-0" becomes 0.
//   - Maps and slices are rebuilt with converted elements; the input is not
//     mutated. Any other type is returned unchanged for DigestJSON to judge.
//
// It returns an error naming the field path for a value with no JCS
// representation: NaN, an infinity, a number that overflows binary64, or an
// integer outside +/-(2^53-1) (which DigestJSON would reject; the helper does
// not guess a string for it).
func CanonicalizeForDigest(value any) (any, error) {
	return canonicalizeForDigest(value, "")
}

func canonicalizeForDigest(value any, path string) (any, error) {
	switch v := value.(type) {
	case json.Number:
		return canonicalizeNumber(v, path)
	case float64:
		return float64ToJCS(v, path)
	case float32:
		return float64ToJCS(float64(v), path)
	case int:
		return checkSafeInteger(int64(v), path)
	case int8, int16, int32, uint8, uint16, uint32:
		return value, nil
	case int64:
		return checkSafeInteger(v, path)
	case uint:
		return checkSafeUnsigned(uint64(v), path)
	case uint64:
		return checkSafeUnsigned(v, path)
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, item := range v {
			converted, err := canonicalizeForDigest(item, joinField(path, key))
			if err != nil {
				return nil, err
			}
			out[key] = converted
		}
		return out, nil
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			converted, err := canonicalizeForDigest(item, fmt.Sprintf("%s[%d]", path, i))
			if err != nil {
				return nil, err
			}
			out[i] = converted
		}
		return out, nil
	default:
		return value, nil
	}
}

func joinField(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func fieldHint(path string) string {
	if path == "" {
		return ""
	}
	return fmt.Sprintf(" (field: %q)", path)
}

func checkSafeInteger(v int64, path string) (any, error) {
	if v > maxSafeInteger || v < -maxSafeInteger {
		return nil, fmt.Errorf("integer %d is outside the JSON-safe range +/-(2^53-1) and has no digest representation%s", v, fieldHint(path))
	}
	return v, nil
}

func checkSafeUnsigned(v uint64, path string) (any, error) {
	if v > maxSafeInteger {
		return nil, fmt.Errorf("integer %d is outside the JSON-safe range +/-(2^53-1) and has no digest representation%s", v, fieldHint(path))
	}
	return v, nil
}

// jsonNumberToken is the RFC 8259 number grammar. strconv accepts more (a
// leading "+", leading zeros, ".5", "1.", hex floats), so a token is checked
// before parsing rather than laundered into a valid value.
var jsonNumberToken = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$`)

func canonicalizeNumber(n json.Number, path string) (any, error) {
	token := string(n)
	if !jsonNumberToken.MatchString(token) {
		return nil, fmt.Errorf("number %q is not a JSON number%s", token, fieldHint(path))
	}
	if !strings.ContainsAny(token, ".eE") {
		// Integer token: keep it inside the safe range, normalize -0.
		parsed, err := strconv.ParseInt(token, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("integer %s is outside the JSON-safe range +/-(2^53-1) and has no digest representation%s", token, fieldHint(path))
		}
		if parsed > maxSafeInteger || parsed < -maxSafeInteger {
			return nil, fmt.Errorf("integer %s is outside the JSON-safe range +/-(2^53-1) and has no digest representation%s", token, fieldHint(path))
		}
		return json.Number(strconv.FormatInt(parsed, 10)), nil
	}
	f, err := strconv.ParseFloat(token, 64)
	if err != nil {
		// The grammar check leaves only ErrRange (overflow to an infinity).
		return nil, fmt.Errorf("number %s overflows binary64 and has no digest representation%s", token, fieldHint(path))
	}
	return float64ToJCS(f, path)
}

// float64ToJCS serializes v per RFC 8785 section 3.2.2.3 (ECMA-262
// 7.1.12.1 Number::toString), naming the field for NaN and infinities.
func float64ToJCS(v float64, path string) (string, error) {
	if v == 0 {
		return "0", nil // also -0.0, RFC 8785 Appendix B
	}
	if math.IsNaN(v) {
		return "", fmt.Errorf("NaN cannot appear in a digest-bearing field%s", fieldHint(path))
	}
	if math.IsInf(v, 0) {
		return "", fmt.Errorf("Infinity cannot appear in a digest-bearing field%s", fieldHint(path))
	}
	// jsontext.AppendFloat is documented as identical to RFC 8785 section
	// 3.2.2.3 for 64-bit precision (and to ECMA-262 7.1.12.1), apart from -0,
	// which is handled above. Callers embed the result as a JSON string.
	return string(jsontext.AppendFloat(nil, v, 64)), nil
}
