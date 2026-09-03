# capsule-emit-go

Go emission core for Agent Action Capsule format 4. The library builds
deterministic, signature-free Capsules and creates independent COSE_Sign1
Producer Envelopes over their raw 32-byte Capsule IDs.

The Go API keeps persistence explicit: `Seal` builds and signs but does not
append a ledger or contact a witness. Applications that need ordered
persistence and witnessed checkpoints compose it with
[`capsule-ledger-go`](https://github.com/ethanyzhang/capsule-ledger-go).

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
// illustrative — see the full example above for setup and error handling
identityCapsule, err := emit.Build(identityInput)
carried, err := emit.Received(input, artifactBytes, "provider-ack")
actionCapsule, err := emit.Build(actionInput)
composed, err := emit.BuildComposition(
    input,
    emit.Who(identityCapsule),
    emit.Can(carried),
    emit.Did(actionCapsule),
)
envelope, err := emit.Sign(composed, identity)
```

The same DID composition can use the high-level signing path:

```go
result, err := emit.Seal(emit.SealInput{
	Capsule:  input,
	Members:  []emit.SlotMember{emit.Did(actionCapsule)},
	Identity: identity,
})
```

Slot helpers reference existing Capsules unchanged. They do not mint or persist
member Capsules.

`Received` and `BuildComposition` may carry provider, model, and runtime
metadata, but reject explicit agent input or output digests. Carried bytes and
slot members already own those construction commitments; mixing authored
payload digests into the same record would make provenance ambiguous.

These functions build records only. They do not append logs, persist Capsules,
deliver to a ledger, retry effects, or authorize signers.

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

## Development

```bash
go fmt ./...
go mod tidy
go vet ./...
go test ./...
go test -race ./...
scripts/check-coverage.sh 90.0
```

## Non-goals

- Executing or retrying provider actions
- Generating business IDs or timestamps
- Persistence, journals, ledgers, or transparency receipts
- Signer authorization policy
- Workflow-engine or provider-specific business semantics

## License

Apache-2.0. The upstream Agent Action Capsule Go dependency is BSD-3-Clause.
