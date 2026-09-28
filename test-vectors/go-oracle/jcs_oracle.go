// SPDX-License-Identifier: Apache-2.0
// Independent-implementation oracle for capsule-emit's JCS vectors: reads a
// JSON array of values on stdin and prints, per value, agent-action-capsule's
// Go reference JCS bytes (hex), their SHA-256, and the evidence-bundle/v2 §9
// URL fragment (go/bundle.EncodeFragment).
//
// Used by the vector generators under test-vectors/*/scripts/. Not part of
// capsule-emit's CI (capsule-emit has no other Go dependency). Setup, from a
// scratch module against a local agent-action-capsule checkout:
//
//	mkdir -p /tmp/aac-jcs-oracle && cd /tmp/aac-jcs-oracle
//	go mod init oracle
//	go mod edit -replace github.com/action-state-group/agent-action-capsule/go=<path-to-agent-action-capsule>/go
//	go mod edit -require github.com/action-state-group/agent-action-capsule/go@v0.0.0
//	cp <this file> main.go
//	go mod tidy
//	echo '[{"a":"Zürich"}]' | go run main.go
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"

	"github.com/action-state-group/agent-action-capsule/go/bundle"
	"github.com/action-state-group/agent-action-capsule/go/canonical"
)

func main() {
	dec := json.NewDecoder(os.Stdin)
	dec.UseNumber()
	var values []interface{}
	if err := dec.Decode(&values); err != nil {
		panic(err)
	}
	out := []map[string]string{}
	for _, v := range values {
		b, err := canonical.JCS(v)
		if err != nil {
			panic(err)
		}
		sum := sha256.Sum256(b)
		frag, err := bundle.EncodeFragment(v)
		if err != nil {
			panic(err)
		}
		out = append(out, map[string]string{"jcs_hex": hex.EncodeToString(b), "sha256": hex.EncodeToString(sum[:]), "fragment": frag})
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(out)
}
