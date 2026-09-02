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
	Model       *Model
	Compute     *ComputeAttestation
	compute     *computeAttestation
}

// Model identifies the provider and model that performed the recorded work.
// Either field may be omitted when the application did not observe it.
type Model struct {
	Provider string
	ModelID  string
}

// ComputeAttestation binds the model invocation to its input, output, and
// runtime. Digests are AAC JSON-DIGEST values produced by DigestJSON.
type ComputeAttestation struct {
	AgentInputDigest  string
	AgentOutputDigest string
	Runtime           string
}

// SealInput is the application-facing one-call producer API. For a regular
// Capsule, non-nil Payload is digest-committed as agent_input_digest and
// non-nil AgentOutput is digest-committed as agent_output_digest. Nil includes
// typed nil pointer, map, and slice values. Use a non-nil
// json.RawMessage("null") to commit explicit JSON null. For a
// composition, set Members to values returned by Who, Can, Did, or Audit;
// Payload and AgentOutput must then be nil.
type SealInput struct {
	Capsule     Input
	Payload     any
	AgentOutput any
	Model       *Model
	Runtime     string
	Members     []SlotMember
	Identity    SigningIdentity
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

// SlotMember binds an existing built or sealed Capsule to one composition
// role. Valid values come from Who, Can, Did, or Audit; its zero value is
// rejected.
type SlotMember struct {
	slot   string
	member compositionCapsule
}

// Result contains a signature-free Capsule and one Producer Envelope.
type Result struct {
	CapsuleID string
	Payload   []byte
	Envelope  []byte
}
