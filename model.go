package emit

import "time"

// Input contains the application-owned values needed to build one Capsule.
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
	compute     *computeAttestation
}

type digestReference struct {
	Type      string
	DigestAlg string
	Digest    string
	Slot      string
}

type computeAttestation struct {
	CarriedArtifact    *digestReference
	CarriedInputDigest string
	ComposedMembers    []digestReference
}

// Disposition records how a decision was disposed.
type Disposition struct {
	Decision      Decision
	Approver      Approver
	HumanDisposed bool
	VerdictClass  VerdictClass
	ReasonDigest  string
}

// Effect describes an external side effect and binds request and response JSON by digest.
type Effect struct {
	Type                 string
	Status               EffectStatus
	IrreversibilityClass IrreversibilityClass
	EffectAttestation    EffectAttestation
	RequestDigest        string
	ResponseDigest       string
	ExternalRef          string
}

// Chain links a format-4 Capsule to one parent Capsule.
type Chain struct {
	ParentCapsuleID string
	Relation        ChainRelation
}

// BuiltPayload is a validated signature-free format-4 Capsule.
type BuiltPayload struct {
	CapsuleID string
	Value     map[string]any
	JSON      []byte
}

// SlotMember binds an existing Capsule to one composition role. Valid values
// come from Who, Can, Did, or Audit; its zero value is rejected.
type SlotMember struct {
	slot   string
	member BuiltPayload
}

// Result contains a signature-free Capsule and one Producer Envelope.
type Result struct {
	CapsuleID string
	Payload   []byte
	Envelope  []byte
}
