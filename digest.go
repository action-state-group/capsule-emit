package producer

import (
	jsonv2 "encoding/json/v2"
	"fmt"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

// DigestJSON marshals value as JSON and returns its AAC JSON-DIGEST: lowercase
// hexadecimal SHA-256 over RFC 8785 JCS. It accepts ordinary JSON-tagged Go
// values and types with custom MarshalJSON methods using encoding/json v2
// semantics. It rejects invalid UTF-8, invalid surrogate pairs, duplicate object
// names, excessive depth, floats, and integers outside the interoperable JSON
// safe range. Negative zero is canonicalized to zero as required by RFC 8785.
func DigestJSON(value any) (string, error) {
	encoded, err := jsonv2.Marshal(value)
	if err != nil {
		return "", fmt.Errorf("marshal JSON digest input: %w", err)
	}
	decoded, err := decodeStrictJSON(encoded)
	if err != nil {
		return "", fmt.Errorf("decode JSON digest input: %w", err)
	}
	digest, err := canonical.JSONDigest(decoded)
	if err != nil {
		return "", fmt.Errorf("canonicalize JSON digest input: %w", err)
	}
	return digest, nil
}
