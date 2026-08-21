package producer

import (
	"strings"
	"testing"
	"time"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const (
	requestDigest  = "1111111111111111111111111111111111111111111111111111111111111111"
	responseDigest = "2222222222222222222222222222222222222222222222222222222222222222"
	parentDigest   = "3333333333333333333333333333333333333333333333333333333333333333"
)

func TestBuildConfirmedDecisionDerivesAssuranceAndCapsuleID(t *testing.T) {
	input := validInput()
	input.Domain = DomainAction
	input.Provenance = ProvenanceGate
	input.EpochID = "policy-2026-08"
	built, err := Build(input)
	require.NoError(t, err)
	payload, encoded := built.Value, built.JSON

	assert.Equal(t, SpecVersion, payload["spec_version"])
	assert.Equal(t, "2026-08-04T14:20:58.025365707Z", payload["timestamp"])
	assert.Equal(t, "action", payload["domain"])
	assert.Equal(t, "gate", payload["provenance"])
	assert.Equal(t, "policy-2026-08", payload["epoch_id"])
	assurance := payload["assurance"].(map[string]any)
	assert.Equal(t, "confirmed", assurance["effect_mode"])
	assert.Equal(t, "self_attested", assurance["attestation_mode"])
	assert.Equal(t, "standalone", assurance["ledger_mode"])

	expectedID, err := canonical.ComputeCapsuleID(payload)
	require.NoError(t, err)
	assert.Equal(t, expectedID, payload["capsule_id"])
	decoded, err := DecodePayload(encoded)
	require.NoError(t, err)
	assert.Equal(t, payload["capsule_id"], decoded["capsule_id"])
}

func TestBuildExcludesChainBlockFromCapsuleIDButDerivesChainedAssurance(t *testing.T) {
	first := validInput()
	first.Chain = &Chain{ParentCapsuleID: parentDigest, Relation: ChainConfirms}
	firstBuilt, err := Build(first)
	require.NoError(t, err)
	firstPayload, encoded := firstBuilt.Value, firstBuilt.JSON
	assert.Contains(t, string(encoded), `"chain"`)
	assurance := firstPayload["assurance"].(map[string]any)
	assert.Equal(t, "chained", assurance["ledger_mode"])

	second := validInput()
	second.Chain = &Chain{ParentCapsuleID: requestDigest, Relation: ChainRelation("com.example.amends")}
	secondBuilt, err := Build(second)
	require.NoError(t, err)
	secondPayload := secondBuilt.Value
	assert.Equal(t, firstPayload["capsule_id"], secondPayload["capsule_id"])
}

func TestBuildPlannedEffectDerivesNotApplicable(t *testing.T) {
	input := validInput()
	input.Effect = &Effect{
		Type:                 "alchemy.query.plan",
		Status:               EffectPlanned,
		IrreversibilityClass: IrreversibilityTwoWay,
	}
	built, err := Build(input)
	require.NoError(t, err)
	payload := built.Value
	assurance := payload["assurance"].(map[string]any)
	assert.Equal(t, "not_applicable", assurance["effect_mode"])
	effect := payload["effect"].(map[string]any)
	assert.NotContains(t, effect, "effect_attestation")
}

func TestBuildAllowsRegistryExtensions(t *testing.T) {
	input := validInput()
	input.Disposition.Decision = Decision("com.example.reviewed")
	input.Disposition.VerdictClass = VerdictClass("com.example.executed")
	input.Effect.Type = "com.example.query"
	input.Effect.IrreversibilityClass = IrreversibilityClass("com.example.reversible")
	input.Effect.EffectAttestation = EffectAttestation("com.example.sensor")
	input.Chain = &Chain{ParentCapsuleID: parentDigest, Relation: ChainRelation("com.example.amends")}

	built, err := Build(input)
	require.NoError(t, err)
	payload := built.Value
	assert.Equal(t, "com.example.amends", payload["chain"].(map[string]any)["relation"])
}

func TestBuildRejectsInvalidInput(t *testing.T) {
	tests := []struct {
		name     string
		mutate   func(*Input)
		contains string
	}{
		{name: "action id", mutate: func(input *Input) { input.ActionID = " " }, contains: "action id"},
		{name: "operator", mutate: func(input *Input) { input.Operator = "" }, contains: "operator"},
		{name: "developer", mutate: func(input *Input) { input.Developer = "" }, contains: "developer"},
		{name: "timestamp", mutate: func(input *Input) { input.Timestamp = time.Time{} }, contains: "timestamp"},
		{name: "action type", mutate: func(input *Input) { input.ActionType = "execute" }, contains: "action type"},
		{name: "decide disposition", mutate: func(input *Input) { input.Disposition = nil }, contains: "requires a disposition"},
		{name: "decision", mutate: func(input *Input) { input.Disposition.Decision = "" }, contains: "decision"},
		{name: "approver", mutate: func(input *Input) { input.Disposition.Approver = "robot" }, contains: "approver"},
		{name: "human honesty", mutate: func(input *Input) { input.Disposition.HumanDisposed = true }, contains: "human approver"},
		{name: "reason digest", mutate: func(input *Input) { input.Disposition.ReasonDigest = "bad" }, contains: "reason digest"},
		{name: "effect type", mutate: func(input *Input) { input.Effect.Type = "" }, contains: "effect type"},
		{name: "effect status", mutate: func(input *Input) { input.Effect.Status = "unknown" }, contains: "effect status"},
		{name: "irreversibility", mutate: func(input *Input) { input.Effect.IrreversibilityClass = "" }, contains: "irreversibility"},
		{name: "request digest", mutate: func(input *Input) { input.Effect.RequestDigest = "BAD" }, contains: "request digest"},
		{name: "response digest", mutate: func(input *Input) { input.Effect.ResponseDigest = "BAD" }, contains: "response digest"},
		{name: "planned digests", mutate: func(input *Input) { input.Effect.Status = EffectPlanned }, contains: "planned effect"},
		{name: "planned attestation", mutate: func(input *Input) {
			input.Effect.Status = EffectPlanned
			input.Effect.RequestDigest = ""
			input.Effect.ResponseDigest = ""
		}, contains: "effect attestation"},
		{name: "dispatched response", mutate: func(input *Input) { input.Effect.Status = EffectDispatched }, contains: "response digest"},
		{name: "dispatched attestation", mutate: func(input *Input) {
			input.Effect.Status = EffectDispatched
			input.Effect.ResponseDigest = ""
			input.Effect.EffectAttestation = ""
		}, contains: "effect attestation"},
		{name: "confirmed response", mutate: func(input *Input) { input.Effect.ResponseDigest = "" }, contains: "response digest"},
		{name: "confirmed attestation", mutate: func(input *Input) { input.Effect.EffectAttestation = "" }, contains: "effect attestation"},
		{name: "failed attestation", mutate: func(input *Input) {
			input.Effect.Status = EffectFailed
			input.Effect.EffectAttestation = ""
		}, contains: "effect attestation"},
		{name: "reverted attestation", mutate: func(input *Input) {
			input.Effect.Status = EffectReverted
			input.Effect.EffectAttestation = ""
		}, contains: "effect attestation"},
		{name: "chain parent", mutate: func(input *Input) {
			input.Chain = &Chain{ParentCapsuleID: "bad", Relation: ChainConfirms}
		}, contains: "parent capsule"},
		{name: "chain relation", mutate: func(input *Input) {
			input.Chain = &Chain{ParentCapsuleID: parentDigest}
		}, contains: "chain relation"},
		{name: "epoch opens", mutate: func(input *Input) {
			input.Chain = &Chain{ParentCapsuleID: parentDigest, Relation: ChainEpochOpens}
		}, contains: "epoch id"},
		{name: "invalid UTF-8 identity", mutate: func(input *Input) {
			input.ActionID = string([]byte{'a', 0xff})
		}, contains: "UTF-8"},
		{name: "invalid UTF-8 effect", mutate: func(input *Input) {
			input.Effect.Type = string([]byte{'t', 0xff})
		}, contains: "UTF-8"},
		{name: "never-dispatch verdict with effect", mutate: func(input *Input) {
			input.Disposition.VerdictClass = VerdictDenied
		}, contains: "verdict_effect_conflict"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			input := validInput()
			test.mutate(&input)
			_, err := Build(input)
			require.ErrorContains(t, err, test.contains)
		})
	}
}

func TestDecodePayloadRejectsInvalidJSONAndNonObject(t *testing.T) {
	_, err := DecodePayload([]byte(`{"broken"`))
	require.Error(t, err)
	_, err = DecodePayload([]byte(`[]`))
	require.ErrorContains(t, err, "not an object")
	_, err = DecodePayload([]byte(`{} null`))
	require.ErrorContains(t, err, "trailing JSON value")
	_, err = DecodePayload([]byte(`{"action_id":"first","action_id":"second"}`))
	require.ErrorContains(t, err, "duplicate object key")
	_, err = DecodePayload([]byte{'{', '"', 'x', '"', ':', '"', 0xff, '"', '}'})
	require.ErrorContains(t, err, "UTF-8")
	deep := `{"value":` + strings.Repeat("[", maxJSONDepth) + strings.Repeat("]", maxJSONDepth) + `}`
	_, err = DecodePayload([]byte(deep))
	require.ErrorContains(t, err, "maximum depth")
}

func TestBuildVerdictEffectOrthogonality(t *testing.T) {
	verdicts := []VerdictClass{
		VerdictBlocked, VerdictHITLDispatched, VerdictDenied, VerdictEngineFailure,
		VerdictDeferred, VerdictNeedsDecision, VerdictExpired, VerdictEscalated, VerdictResolved,
	}
	statuses := []EffectStatus{EffectDispatched, EffectConfirmed, EffectFailed, EffectReverted}
	for _, verdict := range verdicts {
		for _, status := range statuses {
			t.Run(string(verdict)+"/"+string(status), func(t *testing.T) {
				input := validInput()
				input.Disposition.VerdictClass = verdict
				input.Effect.Status = status
				if status != EffectConfirmed {
					input.Effect.ResponseDigest = ""
				}
				_, err := Build(input)
				require.ErrorContains(t, err, "verdict_effect_conflict")
			})
		}
	}

	input := validInput()
	input.Disposition.VerdictClass = VerdictDenied
	input.Effect = &Effect{Type: "test.tool.send", Status: EffectPlanned, IrreversibilityClass: IrreversibilityOneWayConsequential}
	_, err := Build(input)
	require.NoError(t, err)
}

func validInput() Input {
	return Input{
		ActionID:   "operation-123.attempt-1",
		ActionType: ActionTypeDecide,
		Operator:   "local-operator",
		Developer:  "test-producer@1.0.0",
		Timestamp:  time.Date(2026, 8, 4, 14, 20, 58, 25_365_707, time.UTC),
		Disposition: &Disposition{
			Decision:      DecisionAccept,
			Approver:      ApproverPolicy,
			HumanDisposed: false,
			VerdictClass:  VerdictExecuted,
		},
		Effect: &Effect{
			Type:                 "test.tool.send",
			Status:               EffectConfirmed,
			IrreversibilityClass: IrreversibilityOneWayConsequential,
			EffectAttestation:    AttestationGateExecuted,
			RequestDigest:        requestDigest,
			ResponseDigest:       responseDigest,
		},
	}
}
