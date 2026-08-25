// SPDX-License-Identifier: Apache-2.0
// Cross-verifies a capsule-emit-generated producer envelope against
// agent-action-capsule's Go reference verifier (go/envelope).
//
// One-time acceptance check, not part of capsule-emit's CI (capsule-emit
// has no other Go dependency) -- run manually against a local checkout of
// agent-action-capsule main via a replace directive, e.g. from a scratch
// module:
//
//	mkdir -p /tmp/aac-cose-verify && cd /tmp/aac-cose-verify
//	go mod init verify
//	go mod edit -replace github.com/action-state-group/agent-action-capsule/go=<path-to-agent-action-capsule>/go
//	go mod edit -require github.com/action-state-group/agent-action-capsule/go@v0.0.0
//	cp <this file> main.go
//	go mod tidy
//	go run main.go <capsule_id.txt> <envelope.cose>
package main

import (
	"encoding/hex"
	"fmt"
	"os"
	"strings"

	"github.com/action-state-group/agent-action-capsule/go/envelope"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: verify_with_go <capsule_id.txt> <envelope.cose>")
		os.Exit(2)
	}
	idBytes, err := os.ReadFile(os.Args[1])
	if err != nil {
		panic(err)
	}
	capsuleID := strings.TrimSpace(string(idBytes))
	data, err := os.ReadFile(os.Args[2])
	if err != nil {
		panic(err)
	}
	result := envelope.Verify(capsuleID, data)
	if !result.OK {
		fmt.Println("FAIL")
		for _, f := range result.Findings {
			fmt.Printf("  %s: %s\n", f.Code, f.Detail)
		}
		os.Exit(1)
	}
	fmt.Println("OK")
	fmt.Printf("  capsule_id: %s\n", result.CapsuleID)
	fmt.Printf("  public_key: %s\n", hex.EncodeToString(result.PublicKey))
}
