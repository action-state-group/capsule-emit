package jsonl_test

import (
	jsonv2 "encoding/json/v2"
	"strings"
	"testing"

	"github.com/action-state-group/capsule-emit-go/artifact"
	"github.com/stretchr/testify/require"
)

// wireGolden is the byte-identical cross-language JSONL wire line. The same
// string is pinned in capsule-emit-ts/test/artifact/jsonl-wire.test.ts over the
// same synthetic record, proving the Go and TypeScript JSONL stores agree on one
// wire format (snake_case keys, key order, standard base64, and omitempty).
// Opaque synthetic bytes keep producer-format changes from moving the golden.
const wireGolden = `{"capsule_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","capsule":"e30=","producer_envelope":"AAEC/w==","artifacts":[{"name":"payload","binding":"model_attestation.compute_attestation.agent_input_digest","content":"e30K","state":"present","content_sha256":"ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"},{"name":"agent_output","binding":"model_attestation.compute_attestation.agent_output_digest","state":"never_retained"}]}`

func syntheticWireRecord(t *testing.T) artifact.Record {
	t.Helper()
	rec := artifact.Record{
		CapsuleID:        strings.Repeat("a", 64),
		Capsule:          []byte("{}"),
		ProducerEnvelope: []byte{0, 1, 2, 255},
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: []byte("{}\n"), State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, State: artifact.NeverRetained},
		},
	}
	p, err := artifact.Prepare(rec)
	require.NoError(t, err)
	return p
}

// TestJSONLWireGoldenEncode pins the exact bytes the store's Put writes for one
// line (Put marshals the prepared record with encoding/json/v2).
func TestJSONLWireGoldenEncode(t *testing.T) {
	b, err := jsonv2.Marshal(syntheticWireRecord(t))
	require.NoError(t, err)
	require.Equal(t, wireGolden, string(b), "Go JSONL line must match the cross-language golden")
}

// TestJSONLWireGoldenDecode pins that the store parses the golden line (as
// readAt does) back to the original record.
func TestJSONLWireGoldenDecode(t *testing.T) {
	var got artifact.Record
	require.NoError(t, jsonv2.Unmarshal([]byte(wireGolden), &got))
	require.Equal(t, syntheticWireRecord(t), got)
}
