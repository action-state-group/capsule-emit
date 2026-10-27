package emit

import (
	"bytes"
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// The produce cross-check pins Go's output for testdata/produce-cross-check/
// input.json in expected.json. Python's capsule_emit.produce
// (python/tests/test_produce_interop.py) and the Rust crate's produce module
// (rust/capsule-emit/tests/produce_interop.rs) replay the same input and must
// match byte for byte.
// It covers what the frozen format4-interop pack does not: Build with every
// optional member, Carry, BuildComposition with AUDIT, sealing explicit JSON
// null, DigestJSON values, timestamp normalization and rejected inputs.
//
// Regenerate expected.json after an intended output change with
//
//	CAPSULE_EMIT_UPDATE_CROSS_CHECK=1 go test -run TestProduceCrossCheck ./
const produceCrossCheckDir = "testdata/produce-cross-check"

type crossCheckSpec struct {
	SeedHex    string             `json:"seed_hex"`
	Records    []crossCheckRecord `json:"records"`
	Digests    []crossCheckDigest `json:"digests"`
	Timestamps []string           `json:"timestamps"`
	Rejections []crossCheckRecord `json:"rejections"`
}

type crossCheckRecord struct {
	Name         string             `json:"name"`
	Operation    string             `json:"operation"`
	Input        crossCheckInput    `json:"input"`
	ArtifactUTF8 *string            `json:"artifact_utf8"`
	ArtifactHex  *string            `json:"artifact_hex"` // artifact bytes that are not UTF-8
	ArtifactType string             `json:"artifact_type"`
	Payload      json.RawMessage    `json:"payload"`
	PayloadNull  bool               `json:"payload_null"`
	AgentOutput  json.RawMessage    `json:"agent_output"`
	Model        *crossCheckModel   `json:"model"`
	Runtime      string             `json:"runtime"`
	Members      []crossCheckMember `json:"members"`
}

type crossCheckMember struct {
	Slot   string `json:"slot"`
	Record string `json:"record"`
}

type crossCheckModel struct {
	Provider string `json:"provider"`
	ModelID  string `json:"model_id"`
}

type crossCheckReference struct {
	Type            string          `json:"type"`
	DigestAlg       string          `json:"digest_alg"`
	Digest          string          `json:"digest"`
	CitationPurpose string          `json:"citation_purpose"`
	LogCoordinates  json.RawMessage `json:"log_coordinates"`
}

type crossCheckInput struct {
	ActionID    string `json:"action_id"`
	ActionType  string `json:"action_type"`
	Operator    string `json:"operator"`
	Developer   string `json:"developer"`
	Timestamp   string `json:"timestamp"`
	EpochID     string `json:"epoch_id"`
	Domain      string `json:"domain"`
	Provenance  string `json:"provenance"`
	Disposition *struct {
		Decision      string `json:"decision"`
		Approver      string `json:"approver"`
		HumanDisposed bool   `json:"human_disposed"`
		VerdictClass  string `json:"verdict_class"`
		ReasonDigest  string `json:"reason_digest"`
	} `json:"disposition"`
	Effect *struct {
		Type                 string `json:"type"`
		Status               string `json:"status"`
		IrreversibilityClass string `json:"irreversibility_class"`
		EffectAttestation    string `json:"effect_attestation"`
		RequestDigest        string `json:"request_digest"`
		ResponseDigest       string `json:"response_digest"`
		ExternalRef          string `json:"external_ref"`
	} `json:"effect"`
	Chain *struct {
		ParentCapsuleID string `json:"parent_capsule_id"`
		Relation        string `json:"relation"`
	} `json:"chain"`
	References []crossCheckReference `json:"references"`
	Model      *crossCheckModel      `json:"model"`
	Compute    *struct {
		AgentInputDigest  string `json:"agent_input_digest"`
		AgentOutputDigest string `json:"agent_output_digest"`
		Runtime           string `json:"runtime"`
	} `json:"compute"`
	ProvenanceMode *struct {
		Mode             string               `json:"mode"`
		SourceRef        *crossCheckReference `json:"source_ref"`
		SourceAssertedAt string               `json:"source_asserted_at"`
		ImportBatch      string               `json:"import_batch"`
		ImportedAt       string               `json:"imported_at"`
		TimeRung         string               `json:"time_rung"`
	} `json:"provenance_mode"`
	Extensions []struct {
		Name  string          `json:"name"`
		Value json.RawMessage `json:"value"`
		// ValueText, when present, is the extension's exact JSON text, for
		// values a JSON document cannot hold: empty, trailing data or a
		// repeated member name.
		ValueText *string `json:"value_text"`
	} `json:"extensions"`
}

type crossCheckDigest struct {
	Name  string          `json:"name"`
	Value json.RawMessage `json:"value"`
}

type crossCheckExpected struct {
	PublicKeyHex string                     `json:"public_key_hex"`
	Records      []crossCheckExpectedRecord `json:"records"`
	Digests      []crossCheckExpectedDigest `json:"digests"`
	Timestamps   []crossCheckExpectedTime   `json:"timestamps"`
	Rejections   []crossCheckExpectedReject `json:"rejections"`
}

type crossCheckExpectedRecord struct {
	Name        string `json:"name"`
	CapsuleID   string `json:"capsule_id"`
	CapsuleJCS  string `json:"capsule_jcs"`
	EnvelopeHex string `json:"envelope_hex"`
}

type crossCheckExpectedDigest struct {
	Name   string `json:"name"`
	Digest string `json:"digest"`
}

type crossCheckExpectedTime struct {
	Input string `json:"input"`
	Wire  string `json:"wire"`
}

type crossCheckExpectedReject struct {
	Name  string `json:"name"`
	Error string `json:"error"`
}

func (raw crossCheckReference) toReference() (Reference, error) {
	reference := Reference{Type: raw.Type, DigestAlg: raw.DigestAlg, Digest: raw.Digest, CitationPurpose: raw.CitationPurpose}
	if len(raw.LogCoordinates) > 0 {
		decoded, err := decodeStrictJSON(raw.LogCoordinates)
		if err != nil {
			return Reference{}, err
		}
		coordinates, ok := decoded.(map[string]any)
		if !ok {
			return Reference{}, fmt.Errorf("log coordinates must be an object")
		}
		reference.LogCoordinates = coordinates
	}
	return reference, nil
}

func (raw crossCheckInput) toInput() (Input, error) {
	timestamp, err := time.Parse(time.RFC3339Nano, raw.Timestamp)
	if err != nil {
		return Input{}, err
	}
	input := Input{
		ActionID:   raw.ActionID,
		ActionType: ActionType(raw.ActionType),
		Operator:   raw.Operator,
		Developer:  raw.Developer,
		Timestamp:  timestamp,
		EpochID:    raw.EpochID,
		Domain:     Domain(raw.Domain),
		Provenance: Provenance(raw.Provenance),
	}
	if d := raw.Disposition; d != nil {
		input.Disposition = &Disposition{
			Decision: Decision(d.Decision), Approver: Approver(d.Approver), HumanDisposed: d.HumanDisposed,
			VerdictClass: VerdictClass(d.VerdictClass), ReasonDigest: d.ReasonDigest,
		}
	}
	if e := raw.Effect; e != nil {
		input.Effect = &Effect{
			Type: e.Type, Status: EffectStatus(e.Status), IrreversibilityClass: IrreversibilityClass(e.IrreversibilityClass),
			EffectAttestation: EffectAttestation(e.EffectAttestation), RequestDigest: e.RequestDigest,
			ResponseDigest: e.ResponseDigest, ExternalRef: e.ExternalRef,
		}
	}
	if c := raw.Chain; c != nil {
		input.Chain = &Chain{ParentCapsuleID: c.ParentCapsuleID, Relation: ChainRelation(c.Relation)}
	}
	if raw.References != nil {
		input.References = make([]Reference, 0, len(raw.References))
		for _, item := range raw.References {
			reference, err := item.toReference()
			if err != nil {
				return Input{}, err
			}
			input.References = append(input.References, reference)
		}
	}
	if m := raw.Model; m != nil {
		input.Model = &Model{Provider: m.Provider, ModelID: m.ModelID}
	}
	if c := raw.Compute; c != nil {
		input.Compute = &ComputeAttestation{AgentInputDigest: c.AgentInputDigest, AgentOutputDigest: c.AgentOutputDigest, Runtime: c.Runtime}
	}
	if p := raw.ProvenanceMode; p != nil {
		mode := &ProvenanceMode{
			Mode: ProvenanceModeValue(p.Mode), SourceAssertedAt: p.SourceAssertedAt, ImportBatch: p.ImportBatch,
			ImportedAt: p.ImportedAt, TimeRung: TimeRung(p.TimeRung),
		}
		if p.SourceRef != nil {
			source, err := p.SourceRef.toReference()
			if err != nil {
				return Input{}, err
			}
			mode.SourceRef = &source
		}
		input.ProvenanceMode = mode
	}
	for _, extension := range raw.Extensions {
		value := extension.Value
		if extension.ValueText != nil {
			value = json.RawMessage(*extension.ValueText)
		}
		input.Extensions = append(input.Extensions, Extension{Name: extension.Name, Value: value})
	}
	return input, nil
}

func crossCheckJSONValue(raw json.RawMessage, explicitNull bool) (any, error) {
	if explicitNull {
		return json.RawMessage("null"), nil
	}
	if len(raw) == 0 {
		return nil, nil
	}
	return decodeStrictJSON(raw)
}

func runCrossCheckRecord(record crossCheckRecord, identity SigningIdentity, prior map[string]Result) (Result, error) {
	input, err := record.Input.toInput()
	if err != nil {
		return Result{}, err
	}
	var members []SlotMember
	if record.Members != nil {
		members = []SlotMember{}
		for _, member := range record.Members {
			result, ok := prior[member.Record]
			if !ok {
				return Result{}, fmt.Errorf("unknown member record %q", member.Record)
			}
			switch member.Slot {
			case "who":
				members = append(members, Who(result))
			case "can":
				members = append(members, Can(result))
			case "did":
				members = append(members, Did(result))
			case "audit":
				members = append(members, Audit(result))
			default:
				return Result{}, fmt.Errorf("unknown slot %q", member.Slot)
			}
		}
	}
	var artifactBytes []byte
	if record.ArtifactHex != nil {
		if artifactBytes, err = hex.DecodeString(*record.ArtifactHex); err != nil {
			return Result{}, err
		}
	}
	artifact := func() []byte {
		if record.ArtifactUTF8 == nil {
			return artifactBytes
		}
		return []byte(*record.ArtifactUTF8)
	}
	var built BuiltPayload
	switch record.Operation {
	case "seal":
		payload, err := crossCheckJSONValue(record.Payload, record.PayloadNull)
		if err != nil {
			return Result{}, err
		}
		output, err := crossCheckJSONValue(record.AgentOutput, false)
		if err != nil {
			return Result{}, err
		}
		var model *Model
		if record.Model != nil {
			model = &Model{Provider: record.Model.Provider, ModelID: record.Model.ModelID}
		}
		return Seal(SealInput{
			Capsule: input, Payload: payload, AgentOutput: output, Model: model,
			Runtime: record.Runtime, Members: members, Identity: identity,
		})
	case "build":
		built, err = Build(input)
	case "carry":
		built, err = Carry(input, artifact())
	case "received":
		built, err = Received(input, artifact(), record.ArtifactType)
	case "composition":
		built, err = BuildComposition(input, members...)
	default:
		return Result{}, fmt.Errorf("unknown operation %q", record.Operation)
	}
	if err != nil {
		return Result{}, err
	}
	envelope, err := Sign(built, identity)
	if err != nil {
		return Result{}, err
	}
	return Result{CapsuleID: built.CapsuleID, Payload: built.JSON, Envelope: envelope}, nil
}

func produceCrossCheck(t *testing.T) crossCheckExpected {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(produceCrossCheckDir, "input.json"))
	require.NoError(t, err)
	var spec crossCheckSpec
	require.NoError(t, json.Unmarshal(data, &spec))
	seed, err := hex.DecodeString(spec.SeedHex)
	require.NoError(t, err)
	require.Len(t, seed, ed25519.SeedSize)
	identity, err := NewEd25519SigningIdentity(ed25519.NewKeyFromSeed(seed))
	require.NoError(t, err)

	expected := crossCheckExpected{PublicKeyHex: hex.EncodeToString(identity.publicKey)}
	prior := make(map[string]Result, len(spec.Records))
	for _, record := range spec.Records {
		result, err := runCrossCheckRecord(record, identity, prior)
		require.NoError(t, err, record.Name)
		_, err = VerifyCapsule(result.Payload)
		require.NoError(t, err, record.Name)
		_, err = VerifyEnvelope(result.CapsuleID, result.Envelope)
		require.NoError(t, err, record.Name)
		prior[record.Name] = result
		expected.Records = append(expected.Records, crossCheckExpectedRecord{
			Name: record.Name, CapsuleID: result.CapsuleID, CapsuleJCS: string(result.Payload),
			EnvelopeHex: hex.EncodeToString(result.Envelope),
		})
	}
	for _, item := range spec.Digests {
		value, err := decodeStrictJSON(item.Value)
		require.NoError(t, err, item.Name)
		digest, err := DigestJSON(value)
		require.NoError(t, err, item.Name)
		expected.Digests = append(expected.Digests, crossCheckExpectedDigest{Name: item.Name, Digest: digest})
	}
	for _, text := range spec.Timestamps {
		parsed, err := time.Parse(time.RFC3339Nano, text)
		require.NoError(t, err, text)
		expected.Timestamps = append(expected.Timestamps, crossCheckExpectedTime{Input: text, Wire: formatTimestamp(parsed)})
	}
	for _, record := range spec.Rejections {
		_, err := runCrossCheckRecord(record, identity, prior)
		require.Error(t, err, record.Name)
		expected.Rejections = append(expected.Rejections, crossCheckExpectedReject{Name: record.Name, Error: err.Error()})
	}
	return expected
}

func encodeCrossCheckExpected(t *testing.T, expected crossCheckExpected) []byte {
	t.Helper()
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	require.NoError(t, encoder.Encode(expected))
	return buffer.Bytes()
}

// TestProduceCrossCheck regenerates the cross-check output and requires it to
// equal the pinned expected.json, so a Go output change cannot silently
// diverge from what the Python test replays.
func TestProduceCrossCheck(t *testing.T) {
	encoded := encodeCrossCheckExpected(t, produceCrossCheck(t))
	path := filepath.Join(produceCrossCheckDir, "expected.json")
	if os.Getenv("CAPSULE_EMIT_UPDATE_CROSS_CHECK") == "1" {
		require.NoError(t, os.WriteFile(path, encoded, 0o644))
	}
	pinned, err := os.ReadFile(path)
	require.NoError(t, err)
	assert.Equal(t, string(pinned), string(encoded))
}
