package emit

import (
	"encoding/json"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const settlementMember = `{"version":"0","leg":"payee_observed","sealer_role":"payee","received":{"value":"1500000","assetCode":"eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913","assetScale":6}}`

func TestBuildCommitsExtensionIntoCapsuleID(t *testing.T) {
	input := validInput()
	input.Extensions = []Extension{{Name: "settlement", Value: json.RawMessage(settlementMember)}}
	built, err := Build(input)
	require.NoError(t, err)

	settlement := built.Value["settlement"].(map[string]any)
	assert.Equal(t, "payee_observed", settlement["leg"])
	assert.Equal(t, json.Number("6"), settlement["received"].(map[string]any)["assetScale"])

	withoutID := make(map[string]any, len(built.Value))
	for key, value := range built.Value {
		if key != "capsule_id" {
			withoutID[key] = value
		}
	}
	expectedID, err := canonical.ComputeCapsuleID(withoutID)
	require.NoError(t, err)
	assert.Equal(t, expectedID, built.CapsuleID)

	plain, err := Build(validInput())
	require.NoError(t, err)
	assert.NotEqual(t, plain.CapsuleID, built.CapsuleID, "the extension participates in capsule_id")

	result, err := VerifyCapsule(built.JSON)
	require.NoError(t, err)
	assert.True(t, result.OK, "a Capsule with an extension member passes AAC Class 1")
}

func TestBuildExtensionIsByteStableAcrossKeyOrderAndWhitespace(t *testing.T) {
	first := validInput()
	first.Extensions = []Extension{{Name: "settlement", Value: json.RawMessage(`{"leg":"terms","version":"0"}`)}}
	second := validInput()
	second.Extensions = []Extension{{Name: "settlement", Value: json.RawMessage("{ \"version\": \"0\",\n \"leg\": \"terms\" }")}}
	a, err := Build(first)
	require.NoError(t, err)
	b, err := Build(second)
	require.NoError(t, err)
	assert.Equal(t, a.CapsuleID, b.CapsuleID)
	assert.Equal(t, a.JSON, b.JSON)
}

func TestSealCarriesExtensionAndSignsItsCapsuleID(t *testing.T) {
	input := validInput()
	input.Extensions = []Extension{{Name: "settlement", Value: json.RawMessage(settlementMember)}}
	result, err := Seal(SealInput{Capsule: input, Payload: map[string]any{"note": "x"}, Identity: sealIdentityForTest(t)})
	require.NoError(t, err)
	assert.Contains(t, string(result.Payload), `"settlement":{`)
	envelope, err := VerifyEnvelope(result.CapsuleID, result.Envelope)
	require.NoError(t, err)
	assert.True(t, envelope.OK)
}

func TestBuildRejectsInvalidExtensions(t *testing.T) {
	for name, extensions := range map[string][]Extension{
		"base member":           {{Name: "effect", Value: json.RawMessage(`{}`)}},
		"capsule_id":            {{Name: "capsule_id", Value: json.RawMessage(`"x"`)}},
		"unwritten base member": {{Name: "cross_party", Value: json.RawMessage(`{}`)}},
		"bad name":              {{Name: "Settlement", Value: json.RawMessage(`{}`)}},
		"empty name":            {{Name: "", Value: json.RawMessage(`{}`)}},
		"duplicate":             {{Name: "settlement", Value: json.RawMessage(`{}`)}, {Name: "settlement", Value: json.RawMessage(`{}`)}},
		"no value":              {{Name: "settlement"}},
		"null":                  {{Name: "settlement", Value: json.RawMessage(`null`)}},
		"malformed":             {{Name: "settlement", Value: json.RawMessage(`{"a":`)}},
		"trailing value":        {{Name: "settlement", Value: json.RawMessage(`{} {}`)}},
		"duplicate key":         {{Name: "settlement", Value: json.RawMessage(`{"a":"1","a":"2"}`)}},
		"float":                 {{Name: "settlement", Value: json.RawMessage(`{"amount":{"value":1.5}}`)}},
		"exponent":              {{Name: "settlement", Value: json.RawMessage(`[1e3]`)}},
		"unsafe integer":        {{Name: "settlement", Value: json.RawMessage(`{"n":9007199254740993}`)}},
		"invalid utf-8":         {{Name: "settlement", Value: json.RawMessage("\"\xff\"")}},
	} {
		t.Run(name, func(t *testing.T) {
			input := validInput()
			input.Extensions = extensions
			_, err := Build(input)
			assert.Error(t, err)
		})
	}
}
