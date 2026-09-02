package emit

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

var hexDigestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

const maxJSONDepth = 1000

// Build validates input, derives assurance, computes capsule_id with the
// upstream AAC implementation, and returns deterministic JCS payload bytes.
func Build(input Input) (BuiltPayload, error) {
	if err := validateInput(input); err != nil {
		return BuiltPayload{}, err
	}

	payload := map[string]any{
		"spec_version":        SpecVersion,
		"format_version":      FormatVersion,
		"canonicalization_id": CanonicalizationID,
		"action_id":           input.ActionID,
		"action_type":         string(input.ActionType),
		"operator":            input.Operator,
		"developer":           input.Developer,
		"timestamp":           input.Timestamp.UTC().Format(time.RFC3339Nano),
		"assurance":           assuranceMap(input.Effect, input.Chain),
	}
	if input.EpochID != "" {
		payload["epoch_id"] = input.EpochID
	}
	if input.Domain != "" {
		payload["domain"] = string(input.Domain)
	}
	if input.Provenance != "" {
		payload["provenance"] = string(input.Provenance)
	}
	if input.Disposition != nil {
		payload["disposition"] = dispositionMap(*input.Disposition)
	}
	if input.Effect != nil {
		payload["effect"] = effectMap(*input.Effect)
	}
	if input.Chain != nil {
		payload["chain"] = map[string]any{
			"parent_capsule_id": input.Chain.ParentCapsuleID,
			"relation":          string(input.Chain.Relation),
		}
	}
	if attestation := modelAttestationMap(input); attestation != nil {
		payload["model_attestation"] = attestation
	}
	if err := validatePayloadText(payload, "$"); err != nil {
		return BuiltPayload{}, err
	}

	capsuleID, err := canonical.ComputeCapsuleID(payload)
	if err != nil {
		return BuiltPayload{}, fmt.Errorf("compute capsule id: %w", err)
	}
	payload["capsule_id"] = capsuleID
	class1 := verifyClass1(payload)
	if !class1.OK {
		return BuiltPayload{}, fmt.Errorf("built payload failed AAC Class 1: %w", newClass1Error(class1.Findings))
	}
	encoded, err := canonical.JCS(payload)
	if err != nil {
		return BuiltPayload{}, fmt.Errorf("encode canonical capsule payload: %w", err)
	}
	return BuiltPayload{CapsuleID: capsuleID, Value: payload, JSON: encoded}, nil
}

func modelAttestationMap(input Input) map[string]any {
	result := make(map[string]any)
	if input.Model != nil {
		if input.Model.ModelID != "" {
			result["model_id"] = input.Model.ModelID
		}
		if input.Model.Provider != "" {
			result["provider"] = input.Model.Provider
		}
	}
	compute := computeAttestationMap(input.Compute, input.compute)
	if len(compute) > 0 {
		result["compute_attestation"] = compute
	}
	if len(result) == 0 {
		return nil
	}
	return result
}

func computeAttestationMap(attestation *ComputeAttestation, binding *computeAttestation) map[string]any {
	result := make(map[string]any)
	if attestation != nil {
		if attestation.AgentInputDigest != "" {
			result["agent_input_digest"] = attestation.AgentInputDigest
		}
		if attestation.AgentOutputDigest != "" {
			result["agent_output_digest"] = attestation.AgentOutputDigest
		}
		if attestation.Runtime != "" {
			result["runtime"] = attestation.Runtime
		}
	}
	if binding != nil && binding.CarriedArtifact != nil {
		result["carried_artifact"] = digestReferenceMap(*binding.CarriedArtifact)
		result["carried_input_digest"] = binding.CarriedInputDigest
	}
	if binding != nil && len(binding.ComposedMembers) > 0 {
		members := make([]any, 0, len(binding.ComposedMembers))
		for _, member := range binding.ComposedMembers {
			members = append(members, digestReferenceMap(member))
		}
		result["composed_members"] = members
	}
	return result
}

func digestReferenceMap(reference digestReference) map[string]any {
	result := map[string]any{
		"type":       reference.Type,
		"digest_alg": reference.DigestAlg,
		"digest":     reference.Digest,
	}
	if reference.Slot != "" {
		result["slot"] = reference.Slot
	}
	return result
}

func validateInput(input Input) error {
	for name, value := range map[string]string{
		"action id": input.ActionID,
		"operator":  input.Operator,
		"developer": input.Developer,
	} {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	if input.Timestamp.IsZero() {
		return fmt.Errorf("timestamp is required")
	}
	if input.ActionType != ActionTypeFYI && input.ActionType != ActionTypeDecide {
		return fmt.Errorf("action type must be %q or %q", ActionTypeFYI, ActionTypeDecide)
	}
	if input.ActionType == ActionTypeDecide && input.Disposition == nil {
		return fmt.Errorf("decide action requires a disposition")
	}
	if input.Disposition != nil {
		if err := validateDisposition(*input.Disposition); err != nil {
			return err
		}
	}
	if input.Effect != nil {
		if err := validateEffect(*input.Effect); err != nil {
			return err
		}
	}
	if err := validateAttestation(input.Model, input.Compute); err != nil {
		return err
	}
	if input.compute != nil && hasAuthoredDigests(input.Compute) {
		return fmt.Errorf("carried or composed binding must not include agent input or output digest")
	}
	if input.Chain != nil {
		if !hexDigestPattern.MatchString(input.Chain.ParentCapsuleID) {
			return fmt.Errorf("chain parent capsule id must be 64 lowercase hex characters")
		}
		if input.Chain.Relation == "" {
			return fmt.Errorf("chain relation is required")
		}
		if input.Chain.Relation == ChainEpochOpens && input.EpochID == "" {
			return fmt.Errorf("epoch_opens chain requires epoch id")
		}
	}
	return nil
}

func validateAttestation(model *Model, compute *ComputeAttestation) error {
	if model != nil {
		if strings.TrimSpace(model.Provider) == "" && strings.TrimSpace(model.ModelID) == "" {
			return fmt.Errorf("model must include provider or model id")
		}
		if model.Provider != "" && strings.TrimSpace(model.Provider) == "" {
			return fmt.Errorf("model provider must be non-empty when present")
		}
		if model.ModelID != "" && strings.TrimSpace(model.ModelID) == "" {
			return fmt.Errorf("model id must be non-empty when present")
		}
	}
	if compute == nil {
		return nil
	}
	if compute.Runtime != "" && strings.TrimSpace(compute.Runtime) == "" {
		return fmt.Errorf("compute attestation runtime must be non-empty when present")
	}
	if !hasComputeAttestation(compute) {
		return fmt.Errorf("compute attestation must include a digest or runtime")
	}
	for _, field := range []struct {
		name   string
		digest string
	}{
		{name: "agent input", digest: compute.AgentInputDigest},
		{name: "agent output", digest: compute.AgentOutputDigest},
	} {
		if field.digest != "" && !hexDigestPattern.MatchString(field.digest) {
			return fmt.Errorf("compute attestation %s digest must be 64 lowercase hex characters", field.name)
		}
	}
	return nil
}

func hasComputeAttestation(compute *ComputeAttestation) bool {
	return compute != nil && (compute.AgentInputDigest != "" || compute.AgentOutputDigest != "" || compute.Runtime != "")
}

func hasAuthoredDigests(compute *ComputeAttestation) bool {
	return compute != nil && (compute.AgentInputDigest != "" || compute.AgentOutputDigest != "")
}

func validateDisposition(disposition Disposition) error {
	if disposition.Decision == "" {
		return fmt.Errorf("disposition decision is required")
	}
	if disposition.Approver != ApproverHuman && disposition.Approver != ApproverPolicy && disposition.Approver != ApproverCounterparty {
		return fmt.Errorf("disposition approver must be %q, %q, or %q", ApproverHuman, ApproverPolicy, ApproverCounterparty)
	}
	if disposition.HumanDisposed && disposition.Approver != ApproverHuman {
		return fmt.Errorf("human-disposed decision requires human approver")
	}
	if disposition.ReasonDigest != "" && !hexDigestPattern.MatchString(disposition.ReasonDigest) {
		return fmt.Errorf("disposition reason digest must be 64 lowercase hex characters")
	}
	return nil
}

func validateEffect(effect Effect) error {
	if strings.TrimSpace(effect.Type) == "" {
		return fmt.Errorf("effect type is required")
	}
	switch effect.Status {
	case EffectPlanned, EffectDispatched, EffectConfirmed, EffectFailed, EffectReverted:
	default:
		return fmt.Errorf("unsupported effect status %q", effect.Status)
	}
	if effect.IrreversibilityClass == "" {
		return fmt.Errorf("effect irreversibility class is required")
	}
	for name, digest := range map[string]string{
		"request":  effect.RequestDigest,
		"response": effect.ResponseDigest,
	} {
		if digest != "" && !hexDigestPattern.MatchString(digest) {
			return fmt.Errorf("effect %s digest must be 64 lowercase hex characters", name)
		}
	}
	switch effect.Status {
	case EffectPlanned:
		if effect.RequestDigest != "" || effect.ResponseDigest != "" {
			return fmt.Errorf("planned effect must not carry request or response digests")
		}
		if effect.EffectAttestation != "" {
			return fmt.Errorf("planned effect must not carry effect attestation")
		}
	case EffectDispatched:
		if effect.ResponseDigest != "" {
			return fmt.Errorf("dispatched effect must not carry a response digest")
		}
		if effect.EffectAttestation == "" {
			return fmt.Errorf("dispatched effect requires effect attestation")
		}
	case EffectConfirmed:
		if effect.ResponseDigest == "" {
			return fmt.Errorf("confirmed effect requires a response digest")
		}
		if effect.EffectAttestation == "" {
			return fmt.Errorf("confirmed effect requires effect attestation")
		}
	case EffectFailed, EffectReverted:
		if effect.EffectAttestation == "" {
			return fmt.Errorf("%s effect requires effect attestation", effect.Status)
		}
	}
	return nil
}

func validatePayloadText(value any, path string) error {
	switch typed := value.(type) {
	case string:
		if !utf8.ValidString(typed) {
			return fmt.Errorf("payload text at %s must be valid UTF-8", path)
		}
	case map[string]any:
		for key, item := range typed {
			if !utf8.ValidString(key) {
				return fmt.Errorf("payload key at %s must be valid UTF-8", path)
			}
			if err := validatePayloadText(item, path+"."+key); err != nil {
				return err
			}
		}
	case []any:
		for index, item := range typed {
			if err := validatePayloadText(item, fmt.Sprintf("%s[%d]", path, index)); err != nil {
				return err
			}
		}
	}
	return nil
}

func dispositionMap(disposition Disposition) map[string]any {
	result := map[string]any{
		"decision":       string(disposition.Decision),
		"approver":       string(disposition.Approver),
		"human_disposed": disposition.HumanDisposed,
	}
	if disposition.VerdictClass != "" {
		result["verdict_class"] = string(disposition.VerdictClass)
	}
	if disposition.ReasonDigest != "" {
		result["reason_digest"] = disposition.ReasonDigest
	}
	return result
}

func effectMap(effect Effect) map[string]any {
	result := map[string]any{
		"type":                  effect.Type,
		"status":                string(effect.Status),
		"irreversibility_class": string(effect.IrreversibilityClass),
	}
	if effect.EffectAttestation != "" {
		result["effect_attestation"] = string(effect.EffectAttestation)
	}
	if effect.RequestDigest != "" {
		result["request_digest"] = effect.RequestDigest
	}
	if effect.ResponseDigest != "" {
		result["response_digest"] = effect.ResponseDigest
	}
	if effect.ExternalRef != "" {
		result["external_ref"] = effect.ExternalRef
	}
	return result
}

func assuranceMap(effect *Effect, chain *Chain) map[string]any {
	effectMode := EffectModeNotApplicable
	if effect != nil && effect.Status != EffectPlanned {
		effectMode = EffectModeDispatchedUnconfirmed
		if effect.Status == EffectConfirmed {
			effectMode = EffectModeConfirmed
		}
	}
	ledgerMode := LedgerModeStandalone
	if chain != nil {
		ledgerMode = LedgerModeChained
	}
	return map[string]any{
		"effect_mode":      string(effectMode),
		"attestation_mode": string(AttestationModeSelfAttested),
		"ledger_mode":      string(ledgerMode),
	}
}

// DecodePayload decodes payload bytes with json.Number preservation and JCS
// negative-zero normalization.
func DecodePayload(payload []byte) (map[string]any, error) {
	decoded, err := decodeStrictJSON(payload)
	if err != nil {
		return nil, fmt.Errorf("decode capsule payload: %w", err)
	}
	result, ok := decoded.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("capsule payload is not an object")
	}
	return result, nil
}

func decodeStrictJSON(payload []byte) (any, error) {
	if !utf8.Valid(payload) {
		return nil, fmt.Errorf("JSON must be UTF-8")
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	decoded, err := decodeJSONValue(decoder, 1)
	if err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("trailing JSON value")
		}
		return nil, fmt.Errorf("trailing data: %w", err)
	}
	return decoded, nil
}

func decodeJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maxJSONDepth {
		return nil, fmt.Errorf("JSON exceeds maximum depth of %d", maxJSONDepth)
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		// UseNumber routes every number through this branch. RFC 8785 serializes
		// negative zero as 0, while the pinned canonical package emits -0 verbatim.
		if token == json.Number("-0") {
			return json.Number("0"), nil
		}
		return token, nil
	}
	switch delimiter {
	case '{':
		result := make(map[string]any)
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, fmt.Errorf("object key is not a string")
			}
			if _, exists := result[key]; exists {
				return nil, fmt.Errorf("duplicate object key %q", key)
			}
			value, err := decodeJSONValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			result[key] = value
		}
		if _, err := decoder.Token(); err != nil {
			return nil, err
		}
		return result, nil
	case '[':
		var result []any
		for decoder.More() {
			value, err := decodeJSONValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			result = append(result, value)
		}
		if _, err := decoder.Token(); err != nil {
			return nil, err
		}
		return result, nil
	default:
		return nil, fmt.Errorf("unexpected JSON delimiter %q", delimiter)
	}
}
