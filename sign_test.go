package producer

import (
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"strings"
	"testing"

	"github.com/action-state-group/agent-action-capsule/go/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/veraison/go-cose"
)

func TestCreateAndVerifyStatementEndToEnd(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	assert.Len(t, result.CapsuleID, 64)
	assert.NotEmpty(t, result.Payload)
	assert.NotEmpty(t, result.Statement)

	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)
	verified, err := VerifyStatement(result.Statement, verifier, identity.KeyID)
	require.NoError(t, err)
	assert.True(t, verified.Class1.OK)
	assert.Equal(t, result.CapsuleID, verified.Payload["capsule_id"])
	assert.Equal(t, identity.KeyID, verified.KeyID)
}

func TestCreateAndVerifyAllowsRegistryExtensions(t *testing.T) {
	input := validInput()
	input.Disposition.Decision = Decision("com.example.reviewed")
	input.Disposition.VerdictClass = VerdictClass("com.example.executed")
	input.Effect.Type = "com.example.query"
	input.Effect.IrreversibilityClass = IrreversibilityClass("com.example.reversible")
	input.Effect.EffectAttestation = EffectAttestation("com.example.sensor")
	input.Chain = &Chain{ParentCapsuleID: parentDigest, Relation: ChainRelation("com.example.amends")}

	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(input, identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)
	verified, err := VerifyStatement(result.Statement, verifier, identity.KeyID)
	require.NoError(t, err)
	assert.True(t, verified.Class1.OK)
	assert.NotEmpty(t, verified.Class1.Findings, "unknown registry values should be informational, not rejected")
}

func TestCreateRejectsInvalidIdentity(t *testing.T) {
	_, err := Create(validInput(), SigningIdentity{})
	require.ErrorContains(t, err, "signer")

	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	signer, err := cose.NewSigner(cose.AlgorithmEdDSA, privateKey)
	require.NoError(t, err)
	_, err = Create(validInput(), SigningIdentity{Signer: signer})
	require.ErrorContains(t, err, "key id")

	_, err = NewEd25519SigningIdentity(ed25519.PrivateKey("short"))
	require.ErrorContains(t, err, "wrong size")
}

func TestVerifyStatementRejectsTamperingAndWrongTrustInputs(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)

	_, err = VerifyStatement(result.Statement, nil, identity.KeyID)
	require.ErrorContains(t, err, "verifier")
	_, err = VerifyStatement(result.Statement, verifier, nil)
	require.ErrorContains(t, err, "key id")
	_, err = VerifyStatement([]byte("not-cbor"), verifier, identity.KeyID)
	require.ErrorContains(t, err, "decode")
	_, err = VerifyStatement(result.Statement, verifier, []byte("wrong"))
	require.ErrorContains(t, err, "key id")

	tampered := append([]byte(nil), result.Statement...)
	tampered[len(tampered)-1] ^= 0xff
	_, err = VerifyStatement(tampered, verifier, identity.KeyID)
	require.Error(t, err)

	otherPublic, _, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	otherVerifier, err := NewEd25519Verifier(otherPublic)
	require.NoError(t, err)
	_, err = VerifyStatement(result.Statement, otherVerifier, identity.KeyID)
	require.ErrorContains(t, err, "signature")

	_, err = NewEd25519Verifier(ed25519.PublicKey("short"))
	require.ErrorContains(t, err, "wrong size")
}

func TestVerifyStatementRejectsHeaderAndClaimMismatches(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)

	tests := []struct {
		name     string
		mutate   func(*cose.Sign1Message)
		contains string
		resign   bool
	}{
		{name: "content type", contains: "content type", resign: true, mutate: func(message *cose.Sign1Message) {
			message.Headers.Protected[cose.HeaderLabelContentType] = "application/json"
		}},
		{name: "unsupported unprotected", contains: "unsupported unprotected", mutate: func(message *cose.Sign1Message) {
			message.Headers.Unprotected[int64(999)] = "bad"
		}},
		{name: "unsupported critical", contains: "unsupported critical", resign: true, mutate: func(message *cose.Sign1Message) {
			message.Headers.Protected[cose.HeaderLabelCritical] = []any{int64(999)}
			message.Headers.Protected[int64(999)] = "unsupported"
		}},
		{name: "issuer", contains: "issuer claim", resign: true, mutate: func(message *cose.Sign1Message) {
			setClaims(t, message, "wrong", "urn:agent-action-capsule:local-operator:operation-123.attempt-1", "decide", "operation-123.attempt-1")
		}},
		{name: "subject", contains: "subject claim", resign: true, mutate: func(message *cose.Sign1Message) {
			setClaims(t, message, "test-producer@1.0.0", "wrong", "decide", "operation-123.attempt-1")
		}},
		{name: "action type", contains: "capsule_action_type", resign: true, mutate: func(message *cose.Sign1Message) {
			setClaims(t, message, "test-producer@1.0.0", "urn:agent-action-capsule:local-operator:operation-123.attempt-1", "fyi", "operation-123.attempt-1")
		}},
		{name: "decision id", contains: "capsule_decision_id", resign: true, mutate: func(message *cose.Sign1Message) {
			setClaims(t, message, "test-producer@1.0.0", "urn:agent-action-capsule:local-operator:operation-123.attempt-1", "decide", "wrong")
		}},
		{name: "statement type", contains: "capsule_statement_type", resign: true, mutate: func(message *cose.Sign1Message) {
			setAllClaims(t, message, "test-producer@1.0.0", "urn:agent-action-capsule:local-operator:operation-123.attempt-1", "outcome", "decide", "operation-123.attempt-1")
		}},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var message cose.Sign1Message
			require.NoError(t, message.UnmarshalCBOR(result.Statement))
			test.mutate(&message)
			if test.resign {
				message.Signature = nil
				message.Headers.RawProtected = nil
				require.NoError(t, message.Sign(rand.Reader, nil, identity.Signer))
			}
			message.Headers.RawUnprotected = nil
			statement, err := message.MarshalCBOR()
			require.NoError(t, err)
			_, err = VerifyStatement(statement, verifier, identity.KeyID)
			require.ErrorContains(t, err, test.contains)
		})
	}
}

func TestVerifyStatementRejectsAlgorithmProfilePayloadAndClass1Failures(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)

	tests := []struct {
		name     string
		payload  []byte
		contains string
	}{
		{name: "invalid JSON", payload: []byte(`{"broken"`), contains: "decode Capsule payload"},
		{name: "non-object", payload: []byte(`[]`), contains: "not an object"},
		{name: "unsupported profile", payload: []byte(`{"spec_version":"future","format_version":"2"}`), contains: "unsupported Capsule profile"},
		{name: "Class 1", payload: []byte(`{"spec_version":"draft-mih-scitt-agent-action-capsule-02","format_version":"2"}`), contains: "Class 1"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var message cose.Sign1Message
			require.NoError(t, message.UnmarshalCBOR(result.Statement))
			message.Payload = test.payload
			resign(t, &message, identity.Signer)
			statement, err := message.MarshalCBOR()
			require.NoError(t, err)
			_, err = VerifyStatement(statement, verifier, identity.KeyID)
			require.ErrorContains(t, err, test.contains)
		})
	}

	_, err = VerifyStatement(result.Statement, wrongAlgorithmVerifier{}, identity.KeyID)
	require.ErrorContains(t, err, "algorithm")
}

func TestVerifyStatementRejectsAmbiguousJSONPayloads(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)

	tests := []struct {
		name     string
		payload  []byte
		contains string
	}{
		{name: "trailing value", payload: append(append([]byte(nil), result.Payload...), []byte(` null`)...), contains: "trailing JSON value"},
		{name: "trailing garbage", payload: append(append([]byte(nil), result.Payload...), []byte(` garbage`)...), contains: "trailing data"},
		{name: "duplicate key", payload: []byte(strings.Replace(string(result.Payload), `{"action_id":`, `{"action_id":"shadow","action_id":`, 1)), contains: "duplicate object key"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var message cose.Sign1Message
			require.NoError(t, message.UnmarshalCBOR(result.Statement))
			message.Payload = test.payload
			resign(t, &message, identity.Signer)
			statement, err := message.MarshalCBOR()
			require.NoError(t, err)
			_, err = VerifyStatement(statement, verifier, identity.KeyID)
			require.ErrorContains(t, err, test.contains)
		})
	}
}

func TestVerifyStatementReturnsStructuredClass1Failure(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	identity, err := NewEd25519SigningIdentity(privateKey)
	require.NoError(t, err)
	result, err := Create(validInput(), identity)
	require.NoError(t, err)
	verifier, err := NewEd25519Verifier(publicKey)
	require.NoError(t, err)

	var message cose.Sign1Message
	require.NoError(t, message.UnmarshalCBOR(result.Statement))
	payload, err := DecodePayload(message.Payload)
	require.NoError(t, err)
	payload["capsule_id"] = strings.Repeat("0", 64)
	message.Payload, err = canonical.JCS(payload)
	require.NoError(t, err)
	resign(t, &message, identity.Signer)
	statement, err := message.MarshalCBOR()
	require.NoError(t, err)

	verified, err := VerifyStatement(statement, verifier, identity.KeyID)
	require.Error(t, err)
	var class1Error *Class1Error
	require.True(t, errors.As(err, &class1Error))
	require.NotEmpty(t, class1Error.Findings)
	require.NotEmpty(t, verified.Class1.Findings)
	assert.NotContains(t, err.Error(), "0x")
	assert.Contains(t, err.Error(), "code=capsule_id_mismatch")
}

type wrongAlgorithmVerifier struct{}

func (wrongAlgorithmVerifier) Algorithm() cose.Algorithm { return cose.AlgorithmES256 }
func (wrongAlgorithmVerifier) Verify(_, _ []byte) error  { return nil }

func TestStatementHeaderHelpersHandleCBORIntegerForms(t *testing.T) {
	values := []any{int(1), int8(2), int16(3), int32(4), int64(5), uint(6), uint8(7), uint16(8), uint32(9), uint64(10)}
	for index, value := range values {
		actual, ok := integerValue(value)
		assert.True(t, ok)
		assert.Equal(t, int64(index+1), actual)
	}
	_, ok := integerValue("1")
	assert.False(t, ok)
	_, ok = integerValue(uint64(^uint64(0)))
	assert.False(t, ok)
	assert.True(t, containsInt64([]int64{1, 2, 3}, 2))
	assert.False(t, containsInt64([]int64{1, 2, 3}, 4))

	assert.Equal(t, map[any]any{"key": "value"}, claimMap(map[any]any{"key": "value"}))
	assert.Nil(t, claimMap("invalid"))
}

func setClaims(t *testing.T, message *cose.Sign1Message, issuer, subject, actionType, decisionID string) {
	t.Helper()
	setAllClaims(t, message, issuer, subject, StatementTypeAgentAction, actionType, decisionID)
}

func setAllClaims(t *testing.T, message *cose.Sign1Message, issuer, subject, statementType, actionType, decisionID string) {
	t.Helper()
	claims := cose.CWTClaims{
		cwtIssuer:                issuer,
		cwtSubject:               subject,
		"capsule_statement_type": statementType,
		"capsule_action_type":    actionType,
		"capsule_decision_id":    decisionID,
	}
	_, err := message.Headers.Protected.SetCWTClaims(claims)
	require.NoError(t, err)
}

func resign(t *testing.T, message *cose.Sign1Message, signer cose.Signer) {
	t.Helper()
	message.Signature = nil
	message.Headers.RawProtected = nil
	require.NoError(t, message.Sign(rand.Reader, nil, signer))
}
