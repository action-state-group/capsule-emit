package producer

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"fmt"

	"github.com/veraison/go-cose"
)

// SigningIdentity is an immutable signer and matching raw Ed25519 public key.
// Keeping the pair together prevents rotation between header construction and
// signature creation.
type SigningIdentity struct {
	signer    cose.Signer
	publicKey ed25519.PublicKey
}

// NewEd25519SigningIdentity constructs a local Ed25519 signing identity.
func NewEd25519SigningIdentity(privateKey ed25519.PrivateKey) (SigningIdentity, error) {
	if len(privateKey) != ed25519.PrivateKeySize {
		return SigningIdentity{}, fmt.Errorf("Ed25519 private key has wrong size")
	}
	signer, err := cose.NewSigner(cose.AlgorithmEdDSA, privateKey)
	if err != nil {
		return SigningIdentity{}, fmt.Errorf("create Ed25519 COSE signer: %w", err)
	}
	publicKey, ok := privateKey.Public().(ed25519.PublicKey)
	if !ok {
		return SigningIdentity{}, fmt.Errorf("derive Ed25519 public key")
	}
	return NewSigningIdentity(signer, publicKey)
}

// NewSigningIdentity binds an EdDSA signer from a KMS or HSM to its raw
// Ed25519 public key for one immutable Producer Envelope identity.
func NewSigningIdentity(signer cose.Signer, publicKey ed25519.PublicKey) (SigningIdentity, error) {
	if signer == nil || signer.Algorithm() != cose.AlgorithmEdDSA {
		return SigningIdentity{}, fmt.Errorf("Producer Envelope signer must use EdDSA")
	}
	if len(publicKey) != ed25519.PublicKeySize {
		return SigningIdentity{}, fmt.Errorf("Ed25519 public key has wrong size")
	}
	return SigningIdentity{signer: signer, publicKey: append(ed25519.PublicKey(nil), publicKey...)}, nil
}

// Sign creates one Producer Envelope over an already-built Capsule ID.
func Sign(built BuiltPayload, identity SigningIdentity) ([]byte, error) {
	if identity.signer == nil || len(identity.publicKey) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("valid Ed25519 signing identity is required")
	}
	if !hexDigestPattern.MatchString(built.CapsuleID) {
		return nil, fmt.Errorf("Capsule ID must be 64 lowercase hexadecimal characters")
	}
	verified, err := VerifyCapsule(built.JSON)
	if err != nil || verified.CapsuleID == nil || *verified.CapsuleID != built.CapsuleID {
		return nil, fmt.Errorf("built Capsule does not match Capsule ID")
	}
	payload, err := hex.DecodeString(built.CapsuleID)
	if err != nil {
		return nil, fmt.Errorf("decode Capsule ID: %w", err)
	}
	return signCapsuleID(payload, identity)
}

func signCapsuleID(payload []byte, identity SigningIdentity) ([]byte, error) {
	protected := producerProtectedHeaders(identity.publicKey)
	message := cose.NewSign1Message()
	message.Headers.RawProtected = append([]byte{0x58, byte(len(protected))}, protected...)
	message.Headers.Protected.SetAlgorithm(cose.AlgorithmEdDSA)
	message.Payload = payload
	if err := message.Sign(rand.Reader, nil, identity.signer); err != nil {
		return nil, fmt.Errorf("sign Producer Envelope: %w", err)
	}
	verifier, err := cose.NewVerifier(cose.AlgorithmEdDSA, identity.publicKey)
	if err != nil {
		return nil, fmt.Errorf("create Producer Envelope verifier: %w", err)
	}
	if err := message.Verify(nil, verifier); err != nil {
		return nil, fmt.Errorf("Producer Envelope signer does not match public key")
	}
	envelope, err := message.MarshalCBOR()
	if err != nil {
		return nil, fmt.Errorf("encode Producer Envelope: %w", err)
	}
	return envelope, nil
}

// producerProtectedHeaders preserves the byte order frozen by the upstream
// cross-runtime corpus: content type, raw-public-key kid, then EdDSA alg.
func producerProtectedHeaders(publicKey ed25519.PublicKey) []byte {
	protected := []byte{0xa3, 0x03, 0x78, byte(len(ContentType))}
	protected = append(protected, ContentType...)
	protected = append(protected, 0x04, 0x58, ed25519.PublicKeySize)
	protected = append(protected, publicKey...)
	protected = append(protected, 0x01, 0x27)
	return protected
}

// Seal builds one signature-free Capsule and attaches one Producer Envelope.
func Seal(input Input, identity SigningIdentity) (Result, error) {
	built, err := Build(input)
	if err != nil {
		return Result{}, err
	}
	envelope, err := Sign(built, identity)
	if err != nil {
		return Result{}, err
	}
	return Result{CapsuleID: built.CapsuleID, Payload: append([]byte(nil), built.JSON...), Envelope: envelope}, nil
}
