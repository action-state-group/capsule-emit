# capsule-producer-go

[![CI](https://github.com/ethanyzhang/capsule-producer-go/actions/workflows/ci.yml/badge.svg)](https://github.com/ethanyzhang/capsule-producer-go/actions/workflows/ci.yml)

Build and sign Agent Action Capsules from facts your application observed at an external-effect boundary.

This library sits between application adapters and the
[Agent Action Capsule](https://github.com/action-state-group/agent-action-capsule) Go implementation:

```text
HTTP, SQL, file, or message adapter
    captures request and response facts
                    ↓
ordered evidence exchanges
                    ↓
request_digest and response_digest
                    ↓
AAC payload and capsule_id
                    ↓
signed COSE_Sign1 statement
```

It does not execute actions, generate business operation IDs, retry provider calls, persist statements,
recover processes, or maintain a ledger.

## Install

```bash
go get github.com/ethanyzhang/capsule-producer-go
```

## Complete HTTP example

An action fact is an immutable, minimized snapshot of what the application observed. It contains metadata
and content digests, never the raw request or response body.

```go
package main

import (
    "crypto/ed25519"
    "crypto/rand"
    "net/http"
    "time"

    producer "github.com/ethanyzhang/capsule-producer-go"
    "github.com/ethanyzhang/capsule-producer-go/evidence"
    "github.com/ethanyzhang/capsule-producer-go/evidence/httpfact"
)

func createStatement() ([]byte, error) {
    requestBody := []byte(`{"channel":"C123","text":"hello"}`)
    request, err := http.NewRequest(http.MethodPost, "https://slack.com/api/chat.postMessage", nil)
    if err != nil {
        return nil, err
    }
    requestFact, err := httpfact.CaptureRequest(request, requestBody, "slack-api")
    if err != nil {
        return nil, err
    }

    responseBody := []byte(`{"ok":true,"ts":"1722781258.001"}`)
    response := &http.Response{
        StatusCode: http.StatusOK,
        Header:     http.Header{"Content-Type": []string{"application/json"}},
    }
    responseFact, err := httpfact.CaptureResponse(response, responseBody, "ok", true)
    if err != nil {
        return nil, err
    }

    digests, err := evidence.DigestExchanges([]evidence.Exchange{{
        Provider:  "slack",
        Operation: "chat.postMessage",
        Request:   requestFact,
        Response:  responseFact,
    }})
    if err != nil {
        return nil, err
    }

    _, privateKey, err := ed25519.GenerateKey(rand.Reader)
    if err != nil {
        return nil, err
    }
    identity, err := producer.NewEd25519SigningIdentity(privateKey)
    if err != nil {
        return nil, err
    }

    result, err := producer.Create(producer.Input{
        ActionID:   "slack-send-018f6f4d",
        ActionType: producer.ActionTypeDecide,
        Operator:   "local-operator",
        Developer:  "example-agent@1.0.0",
        Timestamp:  time.Now(),
        Disposition: &producer.Disposition{
            Decision:     producer.DecisionAccept,
            Approver:     producer.ApproverPolicy,
            VerdictClass: producer.VerdictExecuted,
        },
        Effect: &producer.Effect{
            Type:                 "example.tool.slack_send",
            Status:               producer.EffectConfirmed,
            IrreversibilityClass: producer.IrreversibilityOneWayConsequential,
            EffectAttestation:    producer.AttestationGateExecuted,
            RequestDigest:        digests.RequestDigest,
            ResponseDigest:       digests.ResponseDigest,
        },
    }, identity)
    if err != nil {
        return nil, err
    }
    return result.Statement, nil
}
```

`result.Payload` is deterministic JCS JSON. `result.Statement` is the payload inside a signed
COSE_Sign1 envelope. `result.CapsuleID` is calculated by the upstream AAC
`canonical.ComputeCapsuleID` implementation.

## Evidence interfaces

A request fact produces the nested request object used as JSON-DIGEST input:

```go
type RequestFact interface {
    RequestEvidence() (map[string]any, error)
}
```

A response fact additionally states whether a response was actually observed:

```go
type ResponseFact interface {
    ResponseEvidence() (map[string]any, error)
    ResponseObserved() bool
}
```

An `Exchange` means one outbound request and its corresponding response:

```go
type Exchange struct {
    Provider  string
    Operation string
    Request   RequestFact
    Response  ResponseFact
}
```

`Provider` and `Operation` belong to the exchange. Request and response implementations must not
repeat those fields.

The included HTTP implementation stores copied strings, lengths, and SHA-256 digests. It never
retains `*http.Request`, `*http.Response`, headers, or body bytes. Other transports can implement
the same interfaces without adding HTTP concepts to the producer.

## Evidence digests

`evidence.DigestExchanges` builds an ordered request envelope and calls the upstream AAC
`canonical.JSONDigest` function:

```text
evidence value
    → AAC Normalize
    → RFC 8785 JCS
    → SHA-256
    → lowercase hexadecimal digest
```

The envelope schema identifier is:

```go
evidence.EffectEvidenceSchemaV1
// urn:capsule-producer:effect-evidence:v1
```

This evidence-envelope convention belongs to this library. It is not an object defined by the AAC
specification. AAC defines the resulting `effect.request_digest` and `effect.response_digest`
bindings.

When any response was not observed, `ResponseKnown` is false and `ResponseDigest` is empty. The
caller can then emit `effect.status=dispatched` without pretending that a response was confirmed.

## Typed protocol values

Closed AAC values such as `ActionType`, `Approver`, and `EffectStatus` are validated. Registry-backed
values such as `Decision`, `VerdictClass`, `IrreversibilityClass`, `EffectAttestation`, and
`ChainRelation` expose the draft-02 seeded constants while still permitting registered extensions.

The producer derives all assurance fields. Callers cannot supply them independently:

```text
no effect or planned effect  → effect_mode=not_applicable
dispatched/failed/reverted   → effect_mode=dispatched_unconfirmed
confirmed response           → effect_mode=confirmed
no chain                     → ledger_mode=standalone
chain present                → ledger_mode=chained
local producer signature     → attestation_mode=self_attested
```

## Capsule ID and chain

AAC calculates `capsule_id` as JSON-DIGEST over the payload after excluding top-level `capsule_id`
and `chain`.

The chain block is nevertheless part of the signed COSE payload. Changing it requires a new
signature. Adding the first chain also changes the correctly derived `assurance.ledger_mode` from
`standalone` to `chained`; that assurance field participates in the Capsule ID. Changing only the
chain block between two already-chained payloads leaves the Capsule ID unchanged.

## Signing and verification

`Create` accepts a `cose.Signer`, so callers can use an in-process key, KMS, or HSM. The convenience
constructor `NewEd25519SigningIdentity` derives the key ID as SHA-256 over the public key.

`VerifyStatement` checks:

- the COSE_Sign1 signature and algorithm;
- protected content type and key ID;
- supported critical and unprotected headers;
- CWT issuer, subject, statement type, action type, and decision ID bindings;
- AAC Class 1 payload verification using the upstream verifier.

Receipt and transparency-log verification are intentionally outside the initial library.

## Non-goals

- Workflow engines and Dapr integration
- Provider request execution
- Retry and idempotency policy
- Action ID generation
- Journals and crash recovery
- Statement storage and Capsule ledgers
- Provider-specific business semantics

## Development

```bash
go fmt ./...
go vet ./...
go test ./...
go test -race ./...
scripts/check-coverage.sh
```

The coverage script enforces a 90% repository statement-coverage floor. GitHub Actions runs the same
checks on every push and pull request.

## License

Apache-2.0. The upstream Agent Action Capsule Go dependency is BSD-3-Clause.
