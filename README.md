# capsule-emit-go

Go emission core for Agent Action Capsule format 4. The library builds
deterministic, signature-free Capsules and creates independent COSE_Sign1
Producer Envelopes over their raw 32-byte Capsule IDs.

The Go API keeps persistence explicit: `Seal` builds and signs but does not
append to a CLL or contact a witness. Applications that need ordered
persistence and witnessed checkpoints compose it with
[`cll-go`](https://github.com/action-state-group/cll-go).

It supports format 4 only. There is no legacy `Create` or signed-payload
statement API, and verification rejects formats 2 and 3.

## Install

Requires Go 1.27 or newer. `DigestJSON` uses `encoding/json/v2`.

```bash
go get github.com/action-state-group/capsule-emit-go
```

## Build, sign, and verify

```go
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"log"
	"time"

	"github.com/action-state-group/capsule-emit-go"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	identity, err := emit.NewEd25519SigningIdentity(privateKey)
	if err != nil {
		return err
	}

	input := emit.Input{
		ActionID:   "send-018f6f4d",
		ActionType: emit.ActionTypeDecide,
		Operator:   "local-operator",
		Developer:  "example-agent@1.0.0",
		Timestamp:  time.Now(),
		Disposition: &emit.Disposition{
			Decision:      emit.DecisionAccept,
			Approver:      emit.ApproverPolicy,
			VerdictClass:  emit.VerdictExecuted,
			HumanDisposed: false,
		},
	}

	result, err := emit.Seal(emit.SealInput{
		Capsule: input,
		Payload: map[string]any{
			"channel": "C123",
			"text":    "hello",
		},
		AgentOutput: map[string]any{"message_id": "M456"},
		Model: &emit.Model{
			Provider: "anthropic",
			ModelID:  "claude-sonnet-4-6",
		},
		Runtime:  "example-runtime",
		Identity: identity,
	})
	if err != nil {
		return err
	}
	class1, err := emit.VerifyCapsule(result.Payload)
	if err != nil || !class1.OK {
		return err
	}
	authenticated, err := emit.VerifyEnvelope(result.CapsuleID, result.Envelope)
	if err != nil {
		return err
	}
	if !bytes.Equal(authenticated.PublicKey, publicKey) {
		return fmt.Errorf("Producer Envelope signer is not authorized")
	}
	return nil
}
```

Production code must handle every error. `VerifyCapsule` validates Capsule
identity and structure; embedded local-only `signature` and `key_id` fields are
not authenticated by it. `VerifyEnvelope` authenticates the public key carried
in the envelope. A consumer of a stored Capsule must also compare that result's
`PublicKey` with the outer `key_id` before trusting the outer field. Whether the
authenticated key is authorized for an operator, developer, or action remains
caller policy.

Multiple signers call `Sign` independently with the same `BuiltPayload`.
Envelope order and signer count do not change the Capsule or its ID.

`Seal` is the recommended application-facing API. It computes
`agent_input_digest` from non-nil `Payload`, computes `agent_output_digest` when
`AgentOutput` is present, maps provider and model ID into
`model_attestation`, maps runtime into `compute_attestation`, then delegates
to `Build` and `Sign`. A nil payload, including a typed nil pointer, map, or
slice, is absent; use a non-nil
`json.RawMessage("null")` to commit explicit JSON null. Raw payload values never
enter the Capsule.

`DigestJSON`, `Build`, `BuildComposition`, and `Sign` remain public stable
primitives for applications that need to control projections, commitments,
construction, or signing separately. Effect request and response digests stay
caller-owned; `Seal` does not replace them with the general agent digests.

AAC draft-04 defines only `fyi` and `decide` as conformant `action_type`
values. The package rejects other values so every `Build` result continues to
pass the current AAC Class 1 verifier.

## Optional artifact storage

`artifact` defines originals, digest bindings, and verification. Two peer backends
provide the same `Store` method contract with explicit transactional persistence
(each is a concrete `*Store` type, not a shared Go interface): `artifact/mysql`
(MySQL 8.4/InnoDB) and `artifact/sqlite` (modernc, pure-Go, single file). The root emit package remains storage-free and imports no
storage driver; an application links only the backend it selects. See
[artifact storage](artifact/README.md) for initialization, limits, trusted-key
policy, retention, and tests. Application workflow state remains caller-owned.

## Persist and append to CLL

`capsule-emit-go` and `cll-go` do not depend on each other. An application may
import both and connect them through their public APIs. Keep the complete
Capsule and Producer Envelope in application-owned storage; CLL stores only the
decoded 32-byte Capsule ID.

```go
package integration

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/hex"
	"fmt"
	"time"

	emit "github.com/action-state-group/capsule-emit-go"
	"github.com/action-state-group/cll-go/cll"
)

// CapsuleStore is application-owned persistence. Its schema and transaction
// model can be adapted to the optional artifact/mysql backend.
type CapsuleStore interface {
	PutCapsule(
		ctx context.Context,
		capsuleID string,
		capsule []byte,
		envelope []byte,
	) error
}

func PersistAndAppend(
	ctx context.Context,
	records CapsuleStore,
	log cll.EntryStore,
	result emit.Result,
	producerPublicKey ed25519.PublicKey,
	observedAt time.Time,
) (cll.AppendResult, error) {
	verified, err := emit.VerifyCapsule(result.Payload)
	if err != nil {
		return cll.AppendResult{}, fmt.Errorf("verify Capsule: %w", err)
	}
	if verified.CapsuleID == nil || *verified.CapsuleID != result.CapsuleID {
		return cll.AppendResult{}, fmt.Errorf("Capsule ID mismatch")
	}
	authenticated, err := emit.VerifyEnvelope(result.CapsuleID, result.Envelope)
	if err != nil {
		return cll.AppendResult{}, err
	}
	if !bytes.Equal(authenticated.PublicKey, producerPublicKey) {
		return cll.AppendResult{}, fmt.Errorf("Producer Envelope signer is not authorized")
	}

	identity, err := hex.DecodeString(result.CapsuleID)
	if err != nil || len(identity) != cll.EntryBytes ||
		hex.EncodeToString(identity) != result.CapsuleID {
		return cll.AppendResult{}, fmt.Errorf("invalid Capsule ID")
	}

	// Persist exact business evidence before publishing its identity to CLL.
	if err := records.PutCapsule(
		ctx,
		result.CapsuleID,
		result.Payload,
		result.Envelope,
	); err != nil {
		return cll.AppendResult{}, err
	}

	return log.Append(ctx, cll.AppendInput{
		Value:      identity,
		AppendedAt: observedAt,
	})
}
```

If application persistence succeeds but the CLL append fails, retry the append
with the same identity. CLL append is idempotent and returns the original entry
and timestamp. The application decides how to make that retry durable, for
example with its own outbox.

Pass any `cll.EntryStore` implementation to this function. Backend selection,
checkpointing, and witness delivery remain entirely in `cll-go`; see its
[backend documentation](https://github.com/action-state-group/cll-go#backends).

## Typed construction

`Carry` binds exact opaque bytes as a generic `foreign-artifact`. `Received`
does the same with a non-empty caller-declared CPB type. Both record the raw
SHA-256 digest as `carried_artifact.digest` and `carried_input_digest`; they
never reinterpret foreign bytes as JSON.

`Who`, `Can`, `Did`, and `Audit` assign existing `BuiltPayload` values or
high-level `Seal` results to the four format-4 composition roles.
`BuildComposition` writes references in the
canonical WHO, CAN, DID, AUDIT order regardless of argument order. It rejects
empty membership, duplicate slots, duplicate Capsule IDs, and members whose
stored bytes do not verify against their claimed IDs.

```go
// Illustrative: inputs and identity are caller-owned; run inside an error-returning function.
identityCapsule, err := emit.Build(identityInput)
if err != nil {
	return err
}
carried, err := emit.Received(input, artifactBytes, "provider-ack")
if err != nil {
	return err
}
actionCapsule, err := emit.Build(actionInput)
if err != nil {
	return err
}
composed, err := emit.BuildComposition(
    input,
    emit.Who(identityCapsule),
    emit.Can(carried),
    emit.Did(actionCapsule),
)
if err != nil {
	return err
}
envelope, err := emit.Sign(composed, identity)
if err != nil {
	return err
}
```

The same DID composition can use the high-level signing path:

```go
result, err := emit.Seal(emit.SealInput{
	Capsule:  input,
	Members:  []emit.SlotMember{emit.Did(actionCapsule)},
	Identity: identity,
})
if err != nil {
	return err
}
```

Slot helpers reference existing Capsules unchanged. They do not mint or persist
member Capsules.

`Received` and `BuildComposition` may carry provider, model, and runtime
metadata, but reject explicit agent input or output digests. Carried bytes and
slot members already own those construction commitments; mixing authored
payload digests into the same record would make provenance ambiguous.

These functions build records only. They do not append logs, persist Capsules,
deliver checkpoints, retry effects, or authorize signers.

## JSON digests

`DigestJSON` accepts any JSON-marshalable value and returns its AAC JSON-DIGEST:
lowercase hexadecimal SHA-256 over RFC 8785 JCS. It rejects duplicate object
names, excessive depth, floats, and integers outside the
interoperable JSON safe range. Marshaling follows Go `encoding/json/v2`
semantics, including strict UTF-8 and Unicode surrogate validation.
Represent fractional quantities as strings or integers in their smallest unit.
When another encoder produces the transmitted bytes, digest the decoded wire
JSON value rather than assuming its output matches `encoding/json/v2`.

```go
// Illustrative: request and response are caller-owned values.
requestDigest, err := emit.DigestJSON(request)
if err != nil {
	return err
}
responseDigest, err := emit.DigestJSON(response)
if err != nil {
	return err
}
```

Callers own the JSON shape. The producer does not impose a transport-specific
wrapper around the values being digested. Assign the results to
`Effect.RequestDigest` and `Effect.ResponseDigest` on an Effect whose type,
status, irreversibility class, and attestation are populated by the caller.

All business IDs and timestamps are caller supplied. Assurance fields are
derived from the supplied effect and chain values. A format-4 Capsule declares
`canonicalization_id: "jcs"`; its Capsule ID commits every field except
top-level `capsule_id` and local-only Producer Envelope fields `signature` and
`key_id`. The `chain` field remains committed.

## Cross-record references

`Input.References` adds draft-04 external citations to the committed payload.
Use `Reference{Type: "agent-action-capsule", DigestAlg: "SHA-256", Digest: id,
CitationPurpose: "responds_to"}` to cite another AAC record. `acted_on` is also
seeded; additional purposes remain informational. References must not duplicate
the Capsule's own chain parent. Foreign artifact types retain their own digest
representation, and their content is not resolved by this producer.

Optional `LogCoordinates` is caller-owned JSON containing `log_id`, `leaf_index`
and `inclusion_proof`. All three are recorded claims; Class 1 does not verify
the proof. A nil `References` slice omits the field; an empty slice emits `[]`.
Those forms have different format-4 IDs because JCS preserves the distinction.

```go
package main

import (
	"fmt"
	"time"

	emit "github.com/action-state-group/capsule-emit-go"
)

func main() {
	if err := run(); err != nil {
		panic(err)
	}
}

func run() error {
	input := emit.Input{
		ActionID: "request/1", ActionType: emit.ActionTypeFYI,
		Operator: "example-org", Developer: "example-agent@v1",
		Timestamp: time.Date(2026, 9, 2, 12, 0, 0, 0, time.UTC),
	}
	request, err := emit.Build(input)
	if err != nil {
		return err
	}
	input.ActionID = "response/1"
	input.References = []emit.Reference{{
		Type: "agent-action-capsule", DigestAlg: "SHA-256",
		Digest: request.CapsuleID, CitationPurpose: "responds_to",
	}}
	response, err := emit.Build(input)
	if err != nil {
		return err
	}
	if _, err := emit.VerifyCapsule(response.JSON); err != nil {
		return err
	}
	fmt.Println(request.CapsuleID, response.CapsuleID)
	return nil
}
```

The reference enters through `Input`, including `SealInput.Capsule` when using
`Seal`. There is no separate reference builder or closed purpose enum.

## Development

`DigestJSON` keeps JCS negative-zero normalization. Python's optional strict
raw-input verification tier is a separate API, not a new Capsule-ID algorithm.

```bash
go fmt ./...
go mod tidy
go vet ./...
go test ./...
go test -race ./...
scripts/check-coverage.sh 90.0
```

`scripts/check-producer-cll-interop.sh` exercises both producers through both
Go/TypeScript CLL append and checkpoint runners, then verifies every checkpoint
with Python. It uses a temporary module so CLL is not a production dependency.
Build the sibling `capsule-emit-ts` and `cll-ts` packages first, and use a Python
environment with `checkpointed-local-log` installed. `PYTHON` selects its
interpreter; sibling locations can be set through `AAC_REPO`,
`CAPSULE_EMIT_TS_ROOT`, `CLL_GO_ROOT` and `CLL_TS_ROOT`.
This is an in-memory, offline integration check, not a witness-delivery or
durable-storage recovery test.

## Emission-core non-goals

- Executing or retrying provider actions
- Generating business IDs or timestamps
- Persistence, journals, local logs, or transparency receipts
- Signer authorization policy
- Workflow-engine or provider-specific business semantics

## License

Apache-2.0. The upstream Agent Action Capsule Go dependency is BSD-3-Clause.
