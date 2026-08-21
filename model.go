package producer

import "time"

// Input contains the application-owned facts needed to build one Capsule.
// ActionID and Timestamp are supplied explicitly so the library never invents
// business identity or wall-clock semantics.
type Input struct {
	ActionID   string
	ActionType ActionType
	Operator   string
	Developer  string
	Timestamp  time.Time
	EpochID    string
	Domain     Domain
	Provenance Provenance

	Disposition *Disposition
	Effect      *Effect
	Chain       *Chain
}

// Disposition records how a decision was disposed.
type Disposition struct {
	Decision      Decision
	Approver      Approver
	HumanDisposed bool
	VerdictClass  VerdictClass
	ReasonDigest  string
}

// Effect describes an external side effect and binds request and response evidence by digest.
type Effect struct {
	Type                 string
	Status               EffectStatus
	IrreversibilityClass IrreversibilityClass
	EffectAttestation    EffectAttestation
	RequestDigest        string
	ResponseDigest       string
	ExternalRef          string
}

// Chain links the signed payload to one parent Capsule.
type Chain struct {
	ParentCapsuleID string
	Relation        ChainRelation
}

// BuiltPayload is a validated AAC payload before COSE signing.
type BuiltPayload struct {
	CapsuleID string
	Value     map[string]any
	JSON      []byte
}

// Result is a complete Capsule payload and its signed COSE_Sign1 statement.
type Result struct {
	CapsuleID string
	Payload   []byte
	Statement []byte
}
