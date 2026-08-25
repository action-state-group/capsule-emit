package producer

import (
	"fmt"
	"strconv"
	"strings"

	upstreamenvelope "github.com/action-state-group/agent-action-capsule/go/envelope"
	"github.com/action-state-group/agent-action-capsule/go/verify"
)

// Class1Error reports structured AAC Class 1 findings.
type Class1Error struct {
	Findings []verify.Finding
}

// Error returns a stable rendering of Class 1 findings.
func (e *Class1Error) Error() string {
	parts := make([]string, 0, len(e.Findings))
	for _, finding := range e.Findings {
		check := "none"
		if finding.Check != nil {
			check = strconv.Itoa(*finding.Check)
		}
		parts = append(parts, fmt.Sprintf("check=%s severity=%s code=%s detail=%s", check, finding.Severity, finding.Code, finding.Detail))
	}
	return "AAC Class 1 verification failed: " + strings.Join(parts, "; ")
}

func newClass1Error(findings []verify.Finding) *Class1Error {
	return &Class1Error{Findings: append([]verify.Finding(nil), findings...)}
}

func verifyClass1(payload map[string]any) verify.VerificationResult {
	return verify.Verify(payload, nil, knownRegistries())
}

func registrySet(values ...string) map[string]bool {
	result := make(map[string]bool, len(values))
	for _, value := range values {
		result[value] = true
	}
	return result
}

var v4Registries = map[string]map[string]bool{
	"verdict_class": registrySet(
		string(VerdictExecuted), string(VerdictBlocked), string(VerdictHITLDispatched),
		string(VerdictDenied), string(VerdictTimeout), string(VerdictErrored),
		string(VerdictEngineFailure), string(VerdictDeferred), string(VerdictNeedsDecision),
		string(VerdictExpired), string(VerdictEscalated), string(VerdictResolved),
		string(VerdictEpochBoundary),
	),
	"disposition.decision": registrySet(string(DecisionAccept), string(DecisionReject), string(DecisionNeedsInput), string(DecisionDeferred)),
	"effect.type":          registrySet("write_order", "send_payment"),
	"irreversibility_class": registrySet(
		string(IrreversibilityTwoWay), string(IrreversibilityOneWayRecoverable),
		string(IrreversibilityOneWayConsequential), string(IrreversibilityOneWayTerminal),
	),
	"effect_attestation": registrySet(string(AttestationGateExecuted), string(AttestationRuntimeClaimed)),
	"chain.relation":     registrySet(string(ChainConfirms), string(ChainSupersedes), string(ChainEpochOpens)),
}

func knownRegistries() map[string]map[string]bool {
	return v4Registries
}

// IsV4IrreversibilityClass reports whether value is seeded by AAC v4.
func IsV4IrreversibilityClass(value IrreversibilityClass) bool {
	return knownRegistries()["irreversibility_class"][string(value)]
}

// VerifyCapsule decodes a format-4 Capsule and performs Class 1 verification.
func VerifyCapsule(data []byte) (verify.VerificationResult, error) {
	payload, err := DecodePayload(data)
	if err != nil {
		return verify.VerificationResult{}, err
	}
	if payload["spec_version"] != SpecVersion || payload["format_version"] != FormatVersion || payload["canonicalization_id"] != CanonicalizationID {
		return verify.VerificationResult{}, fmt.Errorf("unsupported Capsule profile: only AAC format 4 with canonicalization_id %q is supported", CanonicalizationID)
	}
	result := verifyClass1(payload)
	if !result.OK {
		return result, newClass1Error(result.Findings)
	}
	return result, nil
}

// VerifyEnvelope authenticates one Producer Envelope against a Capsule ID.
// The public key in result is evidence, not an authorization decision.
func VerifyEnvelope(capsuleID string, data []byte) (upstreamenvelope.VerificationResult, error) {
	result := upstreamenvelope.Verify(capsuleID, data)
	if !result.OK {
		parts := make([]string, 0, len(result.Findings))
		for _, finding := range result.Findings {
			parts = append(parts, finding.Code+": "+finding.Detail)
		}
		return result, fmt.Errorf("Producer Envelope verification failed: %s", strings.Join(parts, "; "))
	}
	return result, nil
}
