package producer

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"fmt"

	"github.com/veraison/go-cose"
)

const (
	cwtIssuer  int64 = 1
	cwtSubject int64 = 2
)

// SigningIdentity supplies a COSE signer and the public key identifier placed
// in the protected header. The signer may be backed by a local key, KMS, or HSM.
type SigningIdentity struct {
	Signer cose.Signer
	KeyID  []byte
}

// NewEd25519SigningIdentity derives a SHA-256 key ID and COSE signer from an
// Ed25519 private key.
func NewEd25519SigningIdentity(privateKey ed25519.PrivateKey) (SigningIdentity, error) {
	if len(privateKey) != ed25519.PrivateKeySize {
		return SigningIdentity{}, fmt.Errorf("Ed25519 private key has the wrong size")
	}
	signer, err := cose.NewSigner(cose.AlgorithmEdDSA, privateKey)
	if err != nil {
		return SigningIdentity{}, fmt.Errorf("create Ed25519 COSE signer: %w", err)
	}
	publicKey, ok := privateKey.Public().(ed25519.PublicKey)
	if !ok {
		return SigningIdentity{}, fmt.Errorf("derive Ed25519 public key")
	}
	return SigningIdentity{Signer: signer, KeyID: Ed25519KeyID(publicKey)}, nil
}

// Ed25519KeyID returns the SHA-256 key identifier used by the reference profile.
func Ed25519KeyID(publicKey ed25519.PublicKey) []byte {
	digest := sha256.Sum256(publicKey)
	return append([]byte(nil), digest[:]...)
}

// Create builds an AAC payload and returns it in a signed COSE_Sign1 statement.
func Create(input Input, identity SigningIdentity) (Result, error) {
	if identity.Signer == nil {
		return Result{}, fmt.Errorf("COSE signer is required")
	}
	if len(identity.KeyID) == 0 {
		return Result{}, fmt.Errorf("signing key id is required")
	}
	built, err := Build(input)
	if err != nil {
		return Result{}, err
	}

	message := cose.NewSign1Message()
	message.Payload = built.JSON
	message.Headers.Protected.SetAlgorithm(identity.Signer.Algorithm())
	message.Headers.Protected[cose.HeaderLabelKeyID] = append([]byte(nil), identity.KeyID...)
	message.Headers.Protected[cose.HeaderLabelContentType] = ContentType
	claims := cose.CWTClaims{
		cwtIssuer:                input.Developer,
		cwtSubject:               fmt.Sprintf("urn:agent-action-capsule:%s:%s", input.Operator, input.ActionID),
		"capsule_statement_type": StatementTypeAgentAction,
		"capsule_action_type":    string(input.ActionType),
		"capsule_decision_id":    input.ActionID,
	}
	if _, err := message.Headers.Protected.SetCWTClaims(claims); err != nil {
		return Result{}, fmt.Errorf("set protected CWT claims: %w", err)
	}
	if err := message.Sign(rand.Reader, nil, identity.Signer); err != nil {
		return Result{}, fmt.Errorf("sign Capsule statement: %w", err)
	}
	statement, err := message.MarshalCBOR()
	if err != nil {
		return Result{}, fmt.Errorf("encode COSE_Sign1 statement: %w", err)
	}
	return Result{
		CapsuleID: built.CapsuleID,
		Payload:   append([]byte(nil), built.JSON...),
		Statement: statement,
	}, nil
}
