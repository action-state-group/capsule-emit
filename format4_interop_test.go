package emit

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// format4InteropPack is one frozen Python-generated pack and the spec_version
// every record in it carries. The -04 pack shipped in v0.1.0 and is never
// rewritten; the -05 pack beside it is its twin from the same input.json.
type format4InteropPack struct {
	root        string
	specVersion string
}

var format4InteropPacks = []format4InteropPack{
	{root: "testdata/capsule-emit/format4-interop", specVersion: "draft-mih-scitt-agent-action-capsule-04"},
	{root: "testdata/capsule-emit/format4-interop-v05", specVersion: SpecVersion},
}

type format4InteropSpec struct {
	SeedHex     string                    `json:"seed_hex"`
	Timestamp   string                    `json:"timestamp"`
	Operator    string                    `json:"operator"`
	Developer   string                    `json:"developer"`
	Disposition format4InteropDisposition `json:"disposition"`
	Records     []format4InteropRecord    `json:"records"`
}

type format4InteropRecord struct {
	Name         string                 `json:"name"`
	Operation    string                 `json:"operation"`
	ActionID     string                 `json:"action_id"`
	ActionType   string                 `json:"action_type"`
	Verdict      string                 `json:"verdict"`
	Payload      json.RawMessage        `json:"payload"`
	AgentOutput  json.RawMessage        `json:"agent_output"`
	Model        *format4InteropModel   `json:"model"`
	Runtime      string                 `json:"runtime"`
	ArtifactType string                 `json:"artifact_type"`
	ArtifactUTF8 string                 `json:"artifact_utf8"`
	Members      []format4InteropMember `json:"members"`
}

type format4InteropDisposition struct {
	Decision      string `json:"decision"`
	Approver      string `json:"approver"`
	HumanDisposed bool   `json:"human_disposed"`
}

type format4InteropMember struct {
	Slot   string `json:"slot"`
	Record string `json:"record"`
}

type format4InteropModel struct {
	Provider string `json:"provider"`
	ModelID  string `json:"model_id"`
}

type format4InteropExpected struct {
	CapsuleID    string `json:"capsule_id"`
	PublicKeyHex string `json:"public_key_hex"`
}

type format4InteropManifest struct {
	Cases []format4InteropCase `json:"cases"`
}

type format4InteropCase struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

func TestFormat4InteropFrozenChecksums(t *testing.T) {
	for _, pack := range format4InteropPacks {
		t.Run(pack.specVersion, func(t *testing.T) {
			contents, err := os.ReadFile(filepath.Join(pack.root, "SHA256SUMS"))
			require.NoError(t, err)
			for _, line := range strings.Split(strings.TrimSpace(string(contents)), "\n") {
				parts := strings.SplitN(line, "  ", 2)
				require.Len(t, parts, 2)
				data, readErr := os.ReadFile(filepath.Join(pack.root, parts[1]))
				require.NoError(t, readErr)
				digest := sha256.Sum256(data)
				assert.Equal(t, parts[0], hex.EncodeToString(digest[:]), parts[1])
			}
		})
	}
}

func TestFormat4InteropManifestCoversRequiredRecords(t *testing.T) {
	for _, pack := range format4InteropPacks {
		t.Run(pack.specVersion, func(t *testing.T) {
			assertFormat4InteropManifestCoversRequiredRecords(t, pack)
		})
	}
}

func assertFormat4InteropManifestCoversRequiredRecords(t *testing.T, pack format4InteropPack) {
	t.Helper()
	spec := loadFormat4InteropSpec(t, pack)
	manifestData, err := os.ReadFile(filepath.Join(pack.root, "vectors.json"))
	require.NoError(t, err)
	var manifest format4InteropManifest
	require.NoError(t, json.Unmarshal(manifestData, &manifest))

	requiredNames := []string{"authored", "received", "who", "did", "composition"}
	recordNames := make([]string, 0, len(spec.Records))
	for _, record := range spec.Records {
		recordNames = append(recordNames, record.Name)
	}
	require.Equal(t, requiredNames, recordNames)

	manifestCases := make([]format4InteropCase, 0, len(manifest.Cases))
	for _, name := range requiredNames {
		manifestCases = append(manifestCases, format4InteropCase{
			Name: name,
			Path: filepath.ToSlash(filepath.Join("valid", name)),
		})
	}
	require.Equal(t, manifestCases, manifest.Cases)
}

// TestFormat4InteropReplaysPythonVectorsByteForByte proves Go and Python
// capsule-emit produce identical Capsule, ID, and Envelope bytes for both the
// released -04 pack and its -05 twin. The -04 replay pins the in-package
// spec_version override; the -05 replay is the default Seal path.
func TestFormat4InteropReplaysPythonVectorsByteForByte(t *testing.T) {
	for _, pack := range format4InteropPacks {
		t.Run(pack.specVersion, func(t *testing.T) {
			replayFormat4InteropPack(t, pack)
		})
	}
}

func replayFormat4InteropPack(t *testing.T, pack format4InteropPack) {
	t.Helper()
	spec := loadFormat4InteropSpec(t, pack)
	seed, err := hex.DecodeString(spec.SeedHex)
	require.NoError(t, err)
	require.Len(t, seed, ed25519.SeedSize)
	identity, err := NewEd25519SigningIdentity(ed25519.NewKeyFromSeed(seed))
	require.NoError(t, err)
	timestamp, err := time.Parse(time.RFC3339Nano, spec.Timestamp)
	require.NoError(t, err)

	results := make(map[string]Result, len(spec.Records))
	for _, record := range spec.Records {
		result := replayFormat4InteropRecord(t, pack, spec, record, timestamp, identity, results)
		results[record.Name] = result
		assertFormat4InteropResult(t, pack, record.Name, result)
	}
}

func loadFormat4InteropSpec(t *testing.T, pack format4InteropPack) format4InteropSpec {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(pack.root, "input.json"))
	require.NoError(t, err)
	var spec format4InteropSpec
	require.NoError(t, json.Unmarshal(data, &spec))
	return spec
}

func replayFormat4InteropRecord(
	t *testing.T,
	pack format4InteropPack,
	spec format4InteropSpec,
	record format4InteropRecord,
	timestamp time.Time,
	identity SigningIdentity,
	prior map[string]Result,
) Result {
	t.Helper()
	capsule := Input{
		ActionID:   record.ActionID,
		ActionType: ActionType(record.ActionType),
		Operator:   spec.Operator,
		Developer:  spec.Developer,
		Timestamp:  timestamp,
		Disposition: &Disposition{
			Decision:      Decision(spec.Disposition.Decision),
			Approver:      Approver(spec.Disposition.Approver),
			HumanDisposed: spec.Disposition.HumanDisposed,
			VerdictClass:  VerdictClass(record.Verdict),
		},
	}
	if pack.specVersion != SpecVersion {
		capsule.specVersion = pack.specVersion
	}

	switch record.Operation {
	case "seal":
		var model *Model
		if record.Model != nil {
			model = &Model{Provider: record.Model.Provider, ModelID: record.Model.ModelID}
		}
		result, err := Seal(SealInput{
			Capsule:     capsule,
			Payload:     decodeFormat4InteropJSON(t, record.Payload),
			AgentOutput: decodeFormat4InteropJSON(t, record.AgentOutput),
			Model:       model,
			Runtime:     record.Runtime,
			Identity:    identity,
		})
		require.NoError(t, err)
		return result
	case "received":
		built, err := Received(capsule, []byte(record.ArtifactUTF8), record.ArtifactType)
		require.NoError(t, err)
		envelope, err := Sign(built, identity)
		require.NoError(t, err)
		return Result{CapsuleID: built.CapsuleID, Payload: built.JSON, Envelope: envelope}
	case "composition":
		members := make([]SlotMember, 0, len(record.Members))
		for _, member := range record.Members {
			result := requireFormat4InteropResult(t, prior, member.Record)
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
				require.FailNow(t, "unsupported vector slot", member.Slot)
			}
		}
		result, err := Seal(SealInput{Capsule: capsule, Members: members, Identity: identity})
		require.NoError(t, err)
		return result
	default:
		require.FailNow(t, "unsupported vector operation", record.Operation)
		return Result{}
	}
}

func decodeFormat4InteropJSON(t *testing.T, raw json.RawMessage) any {
	t.Helper()
	if len(raw) == 0 {
		return nil
	}
	value, err := decodeStrictJSON(raw)
	require.NoError(t, err)
	return value
}

func requireFormat4InteropResult(t *testing.T, results map[string]Result, name string) Result {
	t.Helper()
	result, ok := results[name]
	require.True(t, ok, "missing prior result %q", name)
	return result
}

func assertFormat4InteropResult(t *testing.T, pack format4InteropPack, name string, result Result) {
	t.Helper()
	caseDir := filepath.Join(pack.root, "valid", name)
	detached, err := os.ReadFile(filepath.Join(caseDir, "capsule.detached.jcs"))
	require.NoError(t, err)
	envelope, err := os.ReadFile(filepath.Join(caseDir, "envelope.cose"))
	require.NoError(t, err)
	expectedData, err := os.ReadFile(filepath.Join(caseDir, "expected.json"))
	require.NoError(t, err)
	var expected format4InteropExpected
	require.NoError(t, json.Unmarshal(expectedData, &expected))

	assert.Equal(t, expected.CapsuleID, result.CapsuleID)
	assert.Equal(t, detached, result.Payload)
	assert.Equal(t, envelope, result.Envelope)

	verified, err := VerifyEnvelope(result.CapsuleID, result.Envelope)
	require.NoError(t, err)
	assert.True(t, verified.OK)
	assert.Equal(t, expected.PublicKeyHex, hex.EncodeToString(verified.PublicKey))

	stored, err := os.ReadFile(filepath.Join(caseDir, "capsule.stored.json"))
	require.NoError(t, err)
	storedVerification, err := VerifyCapsule(stored)
	require.NoError(t, err)
	assert.True(t, storedVerification.OK)
	storedValue, err := DecodePayload(stored)
	require.NoError(t, err)
	assert.Equal(t, result.CapsuleID, storedValue["capsule_id"])
	assert.Equal(t, hex.EncodeToString(result.Envelope), storedValue["signature"])
	assert.Equal(t, expected.PublicKeyHex, storedValue["key_id"])
	assert.Equal(t, pack.specVersion, storedValue["spec_version"])
}
