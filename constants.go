package producer

const (
	// SpecVersion is the AAC profile version implemented by this package.
	SpecVersion = "draft-mih-scitt-agent-action-capsule-02"
	// FormatVersion is the AAC serialization-suite version.
	FormatVersion = "2"
	// ContentType is the COSE payload content type for an AAC statement.
	ContentType = "application/agent-action-capsule+json"
	// StatementTypeAgentAction identifies an action statement in protected CWT claims.
	StatementTypeAgentAction = "agent_action"
)

// ActionType states whether the Capsule is informational or records a decision.
type ActionType string

const (
	ActionTypeFYI    ActionType = "fyi"
	ActionTypeDecide ActionType = "decide"
)

// Decision records how an action was disposed. The registry is extensible;
// these constants are the values seeded by AAC draft-02.
type Decision string

const (
	DecisionAccept     Decision = "accept"
	DecisionReject     Decision = "reject"
	DecisionNeedsInput Decision = "needs_input"
	DecisionDeferred   Decision = "deferred"
)

// Approver identifies who disposed the action. Unlike registry-backed values,
// AAC defines this as a closed enumeration.
type Approver string

const (
	ApproverHuman  Approver = "human"
	ApproverPolicy Approver = "policy"
)

// VerdictClass classifies the terminal or open verdict. The registry is extensible.
type VerdictClass string

const (
	VerdictExecuted       VerdictClass = "executed"
	VerdictBlocked        VerdictClass = "blocked"
	VerdictHITLDispatched VerdictClass = "hitl_dispatched"
	VerdictDenied         VerdictClass = "denied"
	VerdictTimeout        VerdictClass = "timeout"
	VerdictErrored        VerdictClass = "errored"
	VerdictEngineFailure  VerdictClass = "engine_failure"
	VerdictDeferred       VerdictClass = "deferred"
	VerdictNeedsDecision  VerdictClass = "needs_decision"
	VerdictExpired        VerdictClass = "expired"
	VerdictEscalated      VerdictClass = "escalated"
	VerdictResolved       VerdictClass = "resolved"
	VerdictEpochBoundary  VerdictClass = "epoch_boundary"
)

// EffectStatus states how far an external effect progressed.
type EffectStatus string

const (
	EffectPlanned    EffectStatus = "planned"
	EffectDispatched EffectStatus = "dispatched"
	EffectConfirmed  EffectStatus = "confirmed"
	EffectFailed     EffectStatus = "failed"
	EffectReverted   EffectStatus = "reverted"
)

// IrreversibilityClass orders effects by consequence. The registry is extensible.
type IrreversibilityClass string

const (
	IrreversibilityTwoWay              IrreversibilityClass = "two_way"
	IrreversibilityOneWayRecoverable   IrreversibilityClass = "one_way_recoverable"
	IrreversibilityOneWayConsequential IrreversibilityClass = "one_way_consequential"
	IrreversibilityOneWayTerminal      IrreversibilityClass = "one_way_terminal"
)

// EffectAttestation states who vouches for execution. The registry is extensible.
type EffectAttestation string

const (
	AttestationGateExecuted   EffectAttestation = "gate_executed"
	AttestationRuntimeClaimed EffectAttestation = "runtime_claimed"
)

// ChainRelation states how a Capsule relates to its parent. The registry is extensible.
type ChainRelation string

const (
	// ChainConfirms is a library convenience extension. It is not registered
	// by AAC draft-02, so verification reports it as informational.
	ChainConfirms   ChainRelation = "confirms"
	ChainSupersedes ChainRelation = "supersedes"
	ChainEpochOpens ChainRelation = "epoch_opens"
)

// EffectMode is the effect assurance derived from an Effect.
type EffectMode string

// AttestationMode is the producer or transparency attestation tier.
type AttestationMode string

// LedgerMode is the custody tier derived from chain and receipt evidence.
type LedgerMode string

const (
	EffectModeNotApplicable         EffectMode      = "not_applicable"
	EffectModeDispatchedUnconfirmed EffectMode      = "dispatched_unconfirmed"
	EffectModeConfirmed             EffectMode      = "confirmed"
	AttestationModeSelfAttested     AttestationMode = "self_attested"
	AttestationModeAnchored         AttestationMode = "anchored"
	LedgerModeStandalone            LedgerMode      = "standalone"
	LedgerModeChained               LedgerMode      = "chained"
	LedgerModeAnchored              LedgerMode      = "anchored"
)

// Domain describes the epistemic role of the recorded action. The registry is extensible.
type Domain string

const (
	DomainAction    Domain = "action"
	DomainMemory    Domain = "memory"
	DomainReasoning Domain = "reasoning"
)

// Provenance identifies the producer tier. The registry is extensible.
type Provenance string

const (
	ProvenanceGate      Provenance = "gate"
	ProvenanceRuntime   Provenance = "runtime"
	ProvenanceCollector Provenance = "collector"
)
