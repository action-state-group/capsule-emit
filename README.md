# capsule-producer-go

Go producer for Agent Action Capsule format 4. The library builds deterministic,
signature-free Capsules and creates independent COSE_Sign1 Producer Envelopes
over their raw 32-byte Capsule IDs.

It supports format 4 only. There is no legacy `Create` or signed-payload
statement API, and verification rejects formats 2 and 3.

## Install

```bash
go get github.com/ethanyzhang/capsule-producer-go
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

	producer "github.com/ethanyzhang/capsule-producer-go"
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
	identity, err := producer.NewEd25519SigningIdentity(privateKey)
	if err != nil {
		return err
	}

	input := producer.Input{
		ActionID:   "send-018f6f4d",
		ActionType: producer.ActionTypeDecide,
		Operator:   "local-operator",
		Developer:  "example-agent@1.0.0",
		Timestamp:  time.Now(),
		Disposition: &producer.Disposition{
			Decision:      producer.DecisionAccept,
			Approver:      producer.ApproverPolicy,
			VerdictClass:  producer.VerdictExecuted,
			HumanDisposed: false,
		},
	}

	result, err := producer.Seal(input, identity)
	if err != nil {
		return err
	}
	class1, err := producer.VerifyCapsule(result.Payload)
	if err != nil || !class1.OK {
		return err
	}
	authenticated, err := producer.VerifyEnvelope(result.CapsuleID, result.Envelope)
	if err != nil {
		return err
	}
	if !bytes.Equal(authenticated.PublicKey, publicKey) {
		return fmt.Errorf("Producer Envelope signer is not authorized")
	}
	return nil
}
```

Production code must handle every error. `VerifyEnvelope` authenticates the
public key carried in the envelope. Whether that key is authorized for an
operator, developer, or action remains caller policy.

Multiple signers call `Sign` independently with the same `BuiltPayload`.
Envelope order and signer count do not change the Capsule or its ID.

## Typed construction

`Carry` binds exact opaque bytes as a generic `foreign-artifact`. `Received`
does the same with a non-empty caller-declared CPB type. Both record the raw
SHA-256 digest as `carried_artifact.digest` and `carried_input_digest`; they
never reinterpret foreign bytes as JSON.

`Compose` binds one or more existing `BuiltPayload` values by ordered typed
Capsule-ID references. It rejects empty membership and duplicate IDs.

```go
// illustrative — see the full example above for setup and error handling
carried, err := producer.Received(input, artifactBytes, "provider-ack")
composed, err := producer.Compose(input, []producer.BuiltPayload{first, carried})
envelope, err := producer.Sign(composed, identity)
```

These functions build records only. They do not append logs, persist Capsules,
deliver to a ledger, retry effects, or authorize signers.

## Evidence capture

The `evidence` package digests ordered request and response facts. The
`evidence/httpfact` adapter captures detached HTTP metadata and content digests,
never raw body bytes or mutable request and response objects.

All business IDs and timestamps are caller supplied. Assurance fields are
derived from typed effect and chain facts. A format-4 Capsule declares
`canonicalization_id: "jcs"`; its Capsule ID commits every field except the
top-level `capsule_id`, including `chain`.

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
