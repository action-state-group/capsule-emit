package producer

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

var hexDigestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Build validates input, derives assurance, computes capsule_id with the
// upstream AAC implementation, and returns deterministic JCS payload bytes.
func Build(input Input) (BuiltPayload, error) {
	if err := validateInput(input); err != nil {
		return BuiltPayload{}, err
	}

	payload := map[string]any{
		"spec_version":   SpecVersion,
		"format_version": FormatVersion,
		"action_id":      input.ActionID,
		"action_type":    string(input.ActionType),
		"operator":       input.Operator,
		"developer":      input.Developer,
		"timestamp":      input.Timestamp.UTC().Format(time.RFC3339Nano),
		"assurance":      assuranceMap(input.Effect, input.Chain),
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

	capsuleID, err := canonical.ComputeCapsuleID(payload)
	if err != nil {
		return BuiltPayload{}, fmt.Errorf("compute capsule id: %w", err)
	}
	payload["capsule_id"] = capsuleID
	encoded, err := canonical.JCS(payload)
	if err != nil {
		return BuiltPayload{}, fmt.Errorf("encode canonical capsule payload: %w", err)
	}
	return BuiltPayload{CapsuleID: capsuleID, Value: payload, JSON: encoded}, nil
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

func validateDisposition(disposition Disposition) error {
	if disposition.Decision == "" {
		return fmt.Errorf("disposition decision is required")
	}
	if disposition.Approver != ApproverHuman && disposition.Approver != ApproverPolicy {
		return fmt.Errorf("disposition approver must be %q or %q", ApproverHuman, ApproverPolicy)
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

// DecodePayload decodes payload bytes with json.Number preservation.
func DecodePayload(payload []byte) (map[string]any, error) {
	decoded, err := canonicalDecode(payload)
	if err != nil {
		return nil, err
	}
	result, ok := decoded.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("capsule payload is not an object")
	}
	return result, nil
}

func canonicalDecode(payload []byte) (any, error) {
	var value any
	decoder := json.NewDecoder(strings.NewReader(string(payload)))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("decode capsule payload: %w", err)
	}
	return value, nil
}
