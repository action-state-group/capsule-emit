// A test driver compiled in a temporary workspace, keeping CLL out of the
// producer's dependencies. All identities here use a public, fixed test seed.
package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"time"

	emit "github.com/action-state-group/capsule-emit-go"
	"github.com/action-state-group/cll-go/checkpoint"
	"github.com/action-state-group/cll-go/cll"
	"github.com/action-state-group/cll-go/store/memory"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() (err error) {
	if len(os.Args) != 4 {
		return fmt.Errorf("usage: producer-cll <produce|checkpoint> <capsule prefix> <checkpoint path>")
	}
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i)
	}
	key := ed25519.NewKeyFromSeed(seed)
	now := time.Date(2026, 9, 7, 12, 0, 0, 0, time.UTC)
	prefix := os.Args[2]
	if os.Args[1] == "produce" {
		identity, err := emit.NewEd25519SigningIdentity(key)
		if err != nil {
			return err
		}
		result, err := emit.Seal(emit.SealInput{Capsule: emit.Input{ActionID: "producer-cll/example", ActionType: emit.ActionTypeFYI, Operator: "example-org", Developer: "example-agent@v1", Timestamp: now}, Identity: identity})
		if err != nil {
			return err
		}
		if err := os.WriteFile(prefix+".json", result.Payload, 0600); err != nil {
			return err
		}
		return os.WriteFile(prefix+".cose", result.Envelope, 0600)
	}
	if os.Args[1] != "checkpoint" {
		return fmt.Errorf("unknown mode")
	}
	payload, err := os.ReadFile(prefix + ".json")
	if err != nil {
		return err
	}
	verified, err := emit.VerifyCapsule(payload)
	if err != nil {
		return err
	}
	if verified.CapsuleID == nil {
		return fmt.Errorf("missing Capsule ID")
	}
	envelope, err := os.ReadFile(prefix + ".cose")
	if err != nil {
		return err
	}
	author, err := emit.VerifyEnvelope(*verified.CapsuleID, envelope)
	if err != nil {
		return err
	}
	if !bytes.Equal(author.PublicKey, key.Public().(ed25519.PublicKey)) {
		return fmt.Errorf("unexpected producer key")
	}
	id, err := hex.DecodeString(*verified.CapsuleID)
	if err != nil {
		return err
	}
	ctx := context.Background()
	store := memory.New()
	defer func() { err = errors.Join(err, store.Close()) }()
	for _, outcome := range []cll.AppendOutcome{cll.AppendInserted, cll.AppendIdempotent} {
		appended, err := store.Append(ctx, cll.AppendInput{Value: id, AppendedAt: now})
		if err != nil {
			return err
		}
		if appended.Outcome != outcome || appended.Entry.Seq != 1 {
			return fmt.Errorf("append is not dense/idempotent")
		}
	}
	signer, err := checkpoint.NewEd25519Signer(key)
	if err != nil {
		return err
	}
	config := checkpoint.DefaultRunnerConfig("producer-cll")
	config.Cadence.CadenceEntries = 1
	runner, err := checkpoint.NewRunner(config, store, signer)
	if err != nil {
		return err
	}
	if _, err := runner.RunOnce(ctx, now); err != nil {
		return err
	}
	state, err := store.LoadCLL(ctx)
	if err != nil {
		return err
	}
	if state.Checkpoint == nil || state.IndexedSeq != 1 {
		return fmt.Errorf("checkpoint did not include the producer record")
	}
	record, err := checkpoint.ParseRecord(state.Checkpoint.Bytes)
	if err != nil {
		return err
	}
	if err := record.VerifySignature(); err != nil {
		return err
	}
	return os.WriteFile(os.Args[3], state.Checkpoint.Bytes, 0600)
}
