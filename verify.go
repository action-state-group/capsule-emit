package producer

import (
	"bytes"
	"crypto/ed25519"
	"fmt"
	"strconv"
	"strings"

	"github.com/action-state-group/agent-action-capsule/go/verify"
	"github.com/veraison/go-cose"
)

const headerLabelReceipts int64 = 394

// Verification contains the verified payload, key ID, and AAC Class 1 result.
type Verification struct {
	Payload map[string]any
	KeyID   []byte
	Class1  verify.VerificationResult
}

// Class1Error reports structured AAC Class 1 findings.
type Class1Error struct {
	Findings []verify.Finding
}

// Error returns a stable rendering without exposing pointer addresses from
// the upstream finding representation.
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

// NewEd25519Verifier creates a COSE verifier for an Ed25519 public key.
func NewEd25519Verifier(publicKey ed25519.PublicKey) (cose.Verifier, error) {
	if len(publicKey) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("Ed25519 public key has the wrong size")
	}
	verifier, err := cose.NewVerifier(cose.AlgorithmEdDSA, publicKey)
	if err != nil {
		return nil, fmt.Errorf("create Ed25519 COSE verifier: %w", err)
	}
	return verifier, nil
}

// VerifyStatement verifies the COSE signature and protected-header bindings,
// then runs the upstream AAC Class 1 verifier over the signed payload.
func VerifyStatement(statement []byte, verifier cose.Verifier, expectedKeyID []byte) (Verification, error) {
	if verifier == nil {
		return Verification{}, fmt.Errorf("COSE verifier is required")
	}
	if len(expectedKeyID) == 0 {
		return Verification{}, fmt.Errorf("expected key id is required")
	}
	var message cose.Sign1Message
	if err := message.UnmarshalCBOR(statement); err != nil {
		return Verification{}, fmt.Errorf("decode COSE_Sign1 statement: %w", err)
	}
	algorithm, err := message.Headers.Protected.Algorithm()
	if err != nil || algorithm != verifier.Algorithm() {
		return Verification{}, fmt.Errorf("statement algorithm does not match verifier")
	}
	if err := message.Verify(nil, verifier); err != nil {
		return Verification{}, fmt.Errorf("verify COSE_Sign1 signature: %w", err)
	}
	contentType, ok := message.Headers.Protected[cose.HeaderLabelContentType].(string)
	if !ok || contentType != ContentType {
		return Verification{}, fmt.Errorf("statement protected header has the wrong content type")
	}
	keyID, ok := message.Headers.Protected[cose.HeaderLabelKeyID].([]byte)
	if !ok || !bytes.Equal(keyID, expectedKeyID) {
		return Verification{}, fmt.Errorf("statement protected header key id does not match expected key")
	}
	if err := validateStatementHeaders(message); err != nil {
		return Verification{}, err
	}

	payload, err := DecodePayload(message.Payload)
	if err != nil {
		return Verification{}, fmt.Errorf("decode Capsule payload: %w", err)
	}
	if payload["spec_version"] != SpecVersion || payload["format_version"] != FormatVersion {
		return Verification{}, fmt.Errorf("unsupported Capsule profile")
	}
	class1 := verifyClass1(payload)
	verification := Verification{
		Payload: payload,
		KeyID:   append([]byte(nil), keyID...),
		Class1:  class1,
	}
	if !class1.OK {
		return verification, newClass1Error(class1.Findings)
	}
	if err := validateCWTClaims(message, payload); err != nil {
		return Verification{}, err
	}
	return verification, nil
}

func validateStatementHeaders(message cose.Sign1Message) error {
	critical, err := message.Headers.Protected.Critical()
	if err != nil {
		return fmt.Errorf("validate statement critical headers: %w", err)
	}
	for _, label := range critical {
		number, ok := integerValue(label)
		if !ok || !containsInt64([]int64{cose.HeaderLabelAlgorithm, cose.HeaderLabelContentType, cose.HeaderLabelKeyID, cose.HeaderLabelCWTClaims}, number) {
			return fmt.Errorf("statement has an unsupported critical header")
		}
	}
	for label := range message.Headers.Unprotected {
		number, ok := integerValue(label)
		if !ok || number != headerLabelReceipts {
			return fmt.Errorf("statement has an unsupported unprotected header")
		}
	}
	return nil
}

func validateCWTClaims(message cose.Sign1Message, payload map[string]any) error {
	claimsValue, ok := message.Headers.Protected[cose.HeaderLabelCWTClaims]
	if !ok {
		return fmt.Errorf("statement protected header is missing CWT claims")
	}
	claims := claimMap(claimsValue)
	if claims == nil {
		return fmt.Errorf("statement CWT claims must be a map")
	}
	developer, _ := payload["developer"].(string)
	operator, _ := payload["operator"].(string)
	actionID, _ := payload["action_id"].(string)
	actionType, _ := payload["action_type"].(string)
	if claimString(claims, cwtIssuer) != developer || developer == "" {
		return fmt.Errorf("statement issuer claim does not match developer")
	}
	expectedSubject := fmt.Sprintf("urn:agent-action-capsule:%s:%s", operator, actionID)
	if claimString(claims, cwtSubject) != expectedSubject || operator == "" || actionID == "" {
		return fmt.Errorf("statement subject claim does not match payload")
	}
	for claim, expected := range map[string]string{
		"capsule_statement_type": StatementTypeAgentAction,
		"capsule_action_type":    actionType,
		"capsule_decision_id":    actionID,
	} {
		if value, _ := claims[claim].(string); value != expected {
			return fmt.Errorf("statement CWT claim %s does not match payload", claim)
		}
	}
	return nil
}

func knownRegistries() map[string]map[string]bool {
	set := func(values ...string) map[string]bool {
		result := make(map[string]bool, len(values))
		for _, value := range values {
			result[value] = true
		}
		return result
	}
	return map[string]map[string]bool{
		"verdict_class": set(
			string(VerdictExecuted), string(VerdictBlocked), string(VerdictHITLDispatched),
			string(VerdictDenied), string(VerdictTimeout), string(VerdictErrored),
			string(VerdictEngineFailure), string(VerdictDeferred), string(VerdictNeedsDecision),
			string(VerdictExpired), string(VerdictEscalated), string(VerdictResolved),
			string(VerdictEpochBoundary),
		),
		"disposition.decision": set(string(DecisionAccept), string(DecisionReject), string(DecisionNeedsInput), string(DecisionDeferred)),
		"effect.type":          set("write_order", "send_payment"),
		"irreversibility_class": set(
			string(IrreversibilityTwoWay), string(IrreversibilityOneWayRecoverable),
			string(IrreversibilityOneWayConsequential), string(IrreversibilityOneWayTerminal),
		),
		"effect_attestation": set(string(AttestationGateExecuted), string(AttestationRuntimeClaimed)),
		"chain.relation":     set(string(ChainConfirms), string(ChainSupersedes), string(ChainEpochOpens)),
	}
}

func claimMap(value any) map[any]any {
	switch claims := value.(type) {
	case cose.CWTClaims:
		return map[any]any(claims)
	case map[any]any:
		return claims
	default:
		return nil
	}
}

func claimString(claims map[any]any, label int64) string {
	value, _ := anyMapValue(claims, label)
	text, _ := value.(string)
	return text
}

func anyMapValue(values map[any]any, label int64) (any, bool) {
	for key, value := range values {
		if number, ok := integerValue(key); ok && number == label {
			return value, true
		}
	}
	return nil, false
}

func integerValue(value any) (int64, bool) {
	switch number := value.(type) {
	case int:
		return int64(number), true
	case int8:
		return int64(number), true
	case int16:
		return int64(number), true
	case int32:
		return int64(number), true
	case int64:
		return number, true
	case uint:
		return int64(number), uint64(number) <= uint64(^uint64(0)>>1)
	case uint8:
		return int64(number), true
	case uint16:
		return int64(number), true
	case uint32:
		return int64(number), true
	case uint64:
		if number > uint64(^uint64(0)>>1) {
			return 0, false
		}
		return int64(number), true
	default:
		return 0, false
	}
}

func containsInt64(values []int64, target int64) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}
