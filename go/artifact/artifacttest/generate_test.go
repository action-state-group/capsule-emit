package artifacttest

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/json/jsontext"
	jsonv2 "encoding/json/v2"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	emit "github.com/action-state-group/capsule-emit/go"
	"github.com/action-state-group/capsule-emit/go/artifact"
	"github.com/stretchr/testify/require"
	"github.com/veraison/go-cose"
)

// canonicalPath is the language-neutral copy every other runner reads.
const canonicalPath = "../../../test-vectors/artifact-store/vectors.json"

// TestVectorsAreReproducible regenerates the vectors from fixed keys and inputs
// and requires both checked-in copies to match. Set
// UPDATE_ARTIFACT_VECTORS=1 to rewrite them after an intentional change.
func TestVectorsAreReproducible(t *testing.T) {
	generated := generate(t)
	if os.Getenv("UPDATE_ARTIFACT_VECTORS") == "1" {
		require.NoError(t, os.WriteFile("vectors.json", generated, 0o644))
		require.NoError(t, os.MkdirAll(filepath.Dir(canonicalPath), 0o755))
		require.NoError(t, os.WriteFile(canonicalPath, generated, 0o644))
		sum := sha256.Sum256(generated)
		sums := hex.EncodeToString(sum[:]) + "  vectors.json\n"
		require.NoError(t, os.WriteFile(filepath.Join(filepath.Dir(canonicalPath), "SHA256SUMS"), []byte(sums), 0o644))
		return
	}
	canonical, err := os.ReadFile(canonicalPath)
	require.NoError(t, err)
	require.True(t, bytes.Equal(generated, vectorsJSON), "embedded vectors.json is stale; run with UPDATE_ARTIFACT_VECTORS=1")
	require.True(t, bytes.Equal(vectorsJSON, canonical), "embedded copy differs from %s", canonicalPath)
}

type generator struct {
	t       *testing.T
	keys    map[string]ed25519.PrivateKey
	records map[string]artifact.Record
}

func seedKey(b byte) ed25519.PrivateKey {
	return ed25519.NewKeyFromSeed(bytes.Repeat([]byte{b}, ed25519.SeedSize))
}

func (g *generator) seal(key string, input emit.Input, payload, output any) emit.Result {
	identity, err := emit.NewEd25519SigningIdentity(g.keys[key])
	require.NoError(g.t, err)
	if input.Operator == "" {
		input.Operator, input.Developer = "conformance-operator", "conformance-developer"
	}
	if input.Timestamp.IsZero() {
		input.Timestamp = time.Date(2026, 10, 15, 0, 0, 0, 0, time.UTC)
	}
	sealed, err := emit.Seal(emit.SealInput{Capsule: input, Payload: payload, AgentOutput: output, Identity: identity})
	require.NoError(g.t, err)
	return sealed
}

func (g *generator) add(name string, r artifact.Record) artifact.Record {
	if _, dup := g.records[name]; dup {
		g.t.Fatalf("duplicate record %s", name)
	}
	g.records[name] = r
	return r
}

// stored adds the prepared form of an admissible record and its purged form.
func (g *generator) stored(name string) {
	prepared, err := artifact.Prepare(g.records[name])
	require.NoError(g.t, err)
	g.add(name+".stored", prepared)
	purged := prepared
	purged.Artifacts = append([]artifact.Artifact{}, prepared.Artifacts...)
	for i := range purged.Artifacts {
		if purged.Artifacts[i].State == artifact.Present {
			purged.Artifacts[i].Content = nil
			purged.Artifacts[i].State = artifact.Purged
		}
	}
	g.add(name+".purged", purged)
}

// variant copies base and lets mutate change the copy.
func (g *generator) variant(name, base string, mutate func(*artifact.Record)) {
	r := g.records[base]
	r.Artifacts = append([]artifact.Artifact{}, r.Artifacts...)
	mutate(&r)
	g.add(name, r)
}

func content(s string) []byte { return append([]byte{}, s...) }

func digestOf(s string) string { sum := sha256.Sum256([]byte(s)); return hex.EncodeToString(sum[:]) }

func generate(t *testing.T) []byte {
	g := &generator{t: t, records: map[string]artifact.Record{},
		keys: map[string]ed25519.PrivateKey{"trusted": seedKey(1), "rotated": seedKey(2), "other": seedKey(3)}}

	payload := "{\n  \"question\": \"where next?\", \"n\": 2\n}"
	output := `{"next_action":"inspect logs"}`
	note := "not a producer-authenticated assertion"
	basic := g.seal("trusted", emit.Input{ActionID: "conformance-basic", ActionType: emit.ActionTypeFYI},
		json.RawMessage(payload), json.RawMessage(output))
	g.add("basic", artifact.Record{CapsuleID: basic.CapsuleID, Capsule: basic.Payload, ProducerEnvelope: basic.Envelope,
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: content(payload), State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, Content: content(output), State: artifact.Present},
			{Name: "private_note", Content: content(note), State: artifact.Present},
		}})
	g.stored("basic")

	empty := g.seal("trusted", emit.Input{ActionID: "conformance-empty", ActionType: emit.ActionTypeFYI},
		map[string]any{"empty": true}, nil)
	g.add("empty", artifact.Record{CapsuleID: empty.CapsuleID, Capsule: empty.Payload, ProducerEnvelope: empty.Envelope,
		Artifacts: []artifact.Artifact{}})
	g.stored("empty")

	blank := g.seal("trusted", emit.Input{ActionID: "conformance-blank", ActionType: emit.ActionTypeFYI},
		map[string]any{"blank": true}, nil)
	g.add("blank", artifact.Record{CapsuleID: blank.CapsuleID, Capsule: blank.Payload, ProducerEnvelope: blank.Envelope,
		Artifacts: []artifact.Artifact{{Name: "blank", Content: []byte{}, State: artifact.Present}}})
	g.stored("blank")

	request, response := `{"operation":"synthetic"}`, `{"ok":true}`
	requestDigest, err := emit.DigestJSON(json.RawMessage(request))
	require.NoError(t, err)
	responseDigest, err := emit.DigestJSON(json.RawMessage(response))
	require.NoError(t, err)
	effect := g.seal("trusted", emit.Input{ActionID: "conformance-effect", ActionType: emit.ActionTypeDecide,
		Disposition: &emit.Disposition{Decision: emit.DecisionAccept, Approver: emit.ApproverPolicy, VerdictClass: emit.VerdictExecuted},
		Effect: &emit.Effect{Type: "test.tool.send", Status: emit.EffectConfirmed,
			IrreversibilityClass: emit.IrreversibilityOneWayConsequential, EffectAttestation: emit.AttestationGateExecuted,
			RequestDigest: requestDigest, ResponseDigest: responseDigest}},
		map[string]any{"synthetic": true}, nil)
	g.add("effect", artifact.Record{CapsuleID: effect.CapsuleID, Capsule: effect.Payload, ProducerEnvelope: effect.Envelope,
		Artifacts: []artifact.Artifact{
			{Name: "request", Binding: artifact.EffectRequestDigest, Content: content(request), State: artifact.Present},
			{Name: "response", Binding: artifact.EffectResponseDigest, Content: content(response), State: artifact.Present},
		}})
	g.stored("effect")
	g.variant("effect.response_mismatch", "effect", func(r *artifact.Record) {
		r.Artifacts[1] = artifact.Artifact{Name: "response", Binding: artifact.EffectResponseDigest, Content: content(`{"ok":false}`), State: artifact.Present}
	})

	// Index records: generic capsule fields plus a neutral fixture extension
	// whose tags array exercises list matching.
	for _, ix := range []struct {
		name, operator, developer string
		at                        time.Time
		tags                      string
	}{
		{"index.a", "op-a", "dev-1", time.Date(2026, 10, 15, 0, 0, 0, 0, time.UTC), `{"tags":["red","blue"],"level":1}`},
		{"index.b", "op-b", "dev-1", time.Date(2026, 10, 16, 0, 0, 0, 0, time.UTC), `{"tags":["red"],"level":"one"}`},
		{"index.c", "op-a", "dev-2", time.Date(2026, 10, 17, 0, 0, 0, 1000, time.UTC), `{"tags":["blue","blue",7]}`},
	} {
		sealed := g.seal("trusted", emit.Input{ActionID: "conformance-" + ix.name, ActionType: emit.ActionTypeFYI,
			Operator: ix.operator, Developer: ix.developer, Timestamp: ix.at,
			Extensions: []emit.Extension{{Name: "fixture", Value: json.RawMessage(ix.tags)}}},
			map[string]any{"index": ix.name}, nil)
		g.add(ix.name, artifact.Record{CapsuleID: sealed.CapsuleID, Capsule: sealed.Payload, ProducerEnvelope: sealed.Envelope,
			Artifacts: []artifact.Artifact{}})
	}

	rotated := g.seal("rotated", emit.Input{ActionID: "conformance-rotated", ActionType: emit.ActionTypeFYI},
		json.RawMessage(payload), json.RawMessage(output))
	g.add("rotated", artifact.Record{CapsuleID: rotated.CapsuleID, Capsule: rotated.Payload, ProducerEnvelope: rotated.Envelope,
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: content(payload), State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, State: artifact.NeverRetained},
		}})
	g.stored("rotated")

	untrusted := g.seal("other", emit.Input{ActionID: "conformance-untrusted", ActionType: emit.ActionTypeFYI},
		json.RawMessage(payload), nil)
	g.add("untrusted", artifact.Record{CapsuleID: untrusted.CapsuleID, Capsule: untrusted.Payload, ProducerEnvelope: untrusted.Envelope,
		Artifacts: []artifact.Artifact{{Name: "payload", Binding: artifact.PayloadDigest, Content: content(payload), State: artifact.Present}}})

	// Admissible variants of basic.
	g.variant("basic.reordered", "basic", func(r *artifact.Record) {
		a := r.Artifacts
		a[0], a[2] = a[2], a[0]
		a[2].ContentSHA256 = digestOf(payload)
	})
	g.variant("basic.output_never_retained", "basic", func(r *artifact.Record) {
		r.Artifacts[1] = artifact.Artifact{Name: "agent_output", Binding: artifact.AgentOutputDigest, State: artifact.NeverRetained}
	})
	g.stored("basic.output_never_retained")

	// Conflicting variants: each verifies, but its storage checksum differs.
	g.variant("basic.conflict_note", "basic", func(r *artifact.Record) { r.Artifacts[2].Content = content("a different note") })
	g.variant("basic.conflict_extra", "basic", func(r *artifact.Record) {
		r.Artifacts = append(r.Artifacts, artifact.Artifact{Name: "extra", Content: content("x"), State: artifact.Present})
	})
	g.variant("basic.conflict_missing", "basic", func(r *artifact.Record) { r.Artifacts = r.Artifacts[:2] })
	g.variant("basic.conflict_unbound", "basic", func(r *artifact.Record) { r.Artifacts[0].Binding = "" })

	// Rejected variants, one per check.
	g.variant("basic.untrusted_envelope", "basic", func(r *artifact.Record) {
		identity, err := emit.NewEd25519SigningIdentity(g.keys["other"])
		require.NoError(t, err)
		r.ProducerEnvelope = signEnvelope(t, basic, identity)
	})
	g.variant("basic.envelope_other_capsule", "basic", func(r *artifact.Record) { r.ProducerEnvelope = empty.Envelope })
	g.variant("basic.wrong_id", "basic", func(r *artifact.Record) { r.CapsuleID = empty.CapsuleID })
	g.variant("basic.uppercase_id", "basic", func(r *artifact.Record) { r.CapsuleID = strings.ToUpper(basic.CapsuleID) })
	g.variant("basic.capsule_tampered", "basic", func(r *artifact.Record) {
		r.Capsule = bytes.Replace(basic.Payload, []byte("conformance-basic"), []byte("conformance-other"), 1)
	})
	g.variant("basic.payload_mismatch", "basic", func(r *artifact.Record) {
		r.Artifacts[0].Content = content(`{"question":"where next?","n":3}`)
	})
	g.variant("basic.payload_not_json", "basic", func(r *artifact.Record) { r.Artifacts[0].Content = content("not json") })

	// Variants of empty, a record without originals, so that a store that
	// keeps none (the legacy ledgers) runs admission and conflict checks too.
	otherSigner, err := emit.NewEd25519SigningIdentity(g.keys["other"])
	require.NoError(t, err)
	rotatedSigner, err := emit.NewEd25519SigningIdentity(g.keys["rotated"])
	require.NoError(t, err)
	g.variant("empty.rotated_envelope", "empty", func(r *artifact.Record) { r.ProducerEnvelope = signEnvelope(t, empty, rotatedSigner) })
	g.variant("empty.untrusted_envelope", "empty", func(r *artifact.Record) { r.ProducerEnvelope = signEnvelope(t, empty, otherSigner) })
	g.variant("empty.envelope_other_capsule", "empty", func(r *artifact.Record) { r.ProducerEnvelope = basic.Envelope })
	g.variant("empty.wrong_id", "empty", func(r *artifact.Record) { r.CapsuleID = basic.CapsuleID })
	g.variant("empty.capsule_tampered", "empty", func(r *artifact.Record) {
		r.Capsule = bytes.Replace(empty.Payload, []byte("conformance-empty"), []byte("conformance-other"), 1)
	})

	g.variant("basic.sha_mismatch", "basic", func(r *artifact.Record) { r.Artifacts[2].ContentSHA256 = digestOf("something else") })
	g.variant("basic.input_purged", "basic", func(r *artifact.Record) {
		r.Artifacts[2] = artifact.Artifact{Name: "private_note", State: artifact.Purged, ContentSHA256: digestOf(note)}
	})
	g.variant("basic.too_many", "basic", func(r *artifact.Record) {
		for i := len(r.Artifacts); i <= 64; i++ {
			r.Artifacts = append(r.Artifacts, artifact.Artifact{Name: fmt.Sprintf("pad-%02d", i), Content: content("x"), State: artifact.Present})
		}
	})
	g.variant("basic.most_artifacts", "basic", func(r *artifact.Record) {
		for i := len(r.Artifacts); i < 64; i++ {
			r.Artifacts = append(r.Artifacts, artifact.Artifact{Name: fmt.Sprintf("pad-%02d", i), Content: content("x"), State: artifact.Present})
		}
	})
	g.stored("basic.most_artifacts")
	// Prepare checks artifacts in record order; the first failure decides.
	g.variant("basic.corrupt_then_purged", "basic", func(r *artifact.Record) {
		r.Artifacts[0].ContentSHA256 = digestOf("something else")
		r.Artifacts[2] = artifact.Artifact{Name: "private_note", State: artifact.Purged, ContentSHA256: digestOf(note)}
	})
	g.variant("basic.purged_then_corrupt", "basic", func(r *artifact.Record) {
		r.Artifacts[0] = artifact.Artifact{Name: "payload", Binding: artifact.PayloadDigest, State: artifact.Purged, ContentSHA256: digestOf(payload)}
		r.Artifacts[2].ContentSHA256 = digestOf("something else")
	})
	g.variant("basic.duplicate_name", "basic", func(r *artifact.Record) {
		r.Artifacts = append(r.Artifacts, artifact.Artifact{Name: "private_note", Content: content("again"), State: artifact.Present})
	})
	g.variant("basic.bad_name", "basic", func(r *artifact.Record) { r.Artifacts[2].Name = "-note" })
	g.variant("basic.never_retained_with_content", "basic", func(r *artifact.Record) { r.Artifacts[2].State = artifact.NeverRetained })
	g.variant("basic.present_without_content", "basic", func(r *artifact.Record) { r.Artifacts[2].Content = nil })
	g.variant("basic.unsupported_binding", "basic", func(r *artifact.Record) { r.Artifacts[2].Binding = "effect.digest" })
	g.variant("basic.binding_absent_in_capsule", "basic", func(r *artifact.Record) {
		r.Artifacts[2].Binding = artifact.EffectRequestDigest
	})
	g.variant("basic.unknown_state", "basic", func(r *artifact.Record) { r.Artifacts[2].State = "archived" })

	// Padding records (Evidence Layer -00, "What a Checkpoint Reveals"): log
	// leaves that are not Capsules, admitted by their closed shape. Each
	// rejected variant recomputes its own capsule_id and carries an envelope
	// over it, so only the named rule fails.
	g.padding("padding", "trusted", map[string]any{"store_nonce": strings.Repeat("11", 32)}, nil)
	g.stored("padding")
	g.padding("padding.with_note", "trusted", map[string]any{"store_nonce": strings.Repeat("22", 32)},
		[]artifact.Artifact{{Name: "private_note", Content: content(note), State: artifact.Present}})
	g.stored("padding.with_note")
	g.padding("padding.rotated", "rotated", map[string]any{"store_nonce": strings.Repeat("33", 32)}, nil)
	g.padding("padding.untrusted", "other", map[string]any{"store_nonce": strings.Repeat("44", 32)}, nil)
	g.padding("padding.bound", "trusted", map[string]any{"store_nonce": strings.Repeat("55", 32)},
		[]artifact.Artifact{{Name: "payload", Binding: artifact.PayloadDigest, Content: content(payload), State: artifact.Present}})
	g.padding("padding.capsule_profile", "trusted", map[string]any{"store_nonce": strings.Repeat("66", 32),
		"format_version": "4", "canonicalization_id": "jcs"}, nil)
	g.padding("padding.extra_member", "trusted", map[string]any{"store_nonce": strings.Repeat("77", 32), "links": []any{}}, nil)
	g.padding("padding.epistemic_type", "trusted", map[string]any{"store_nonce": strings.Repeat("88", 32),
		"epistemic_type": "observed_event"}, nil)
	g.padding("padding.nonce_uppercase", "trusted", map[string]any{"store_nonce": strings.Repeat("AB", 32)}, nil)
	g.padding("padding.nonce_missing", "trusted", map[string]any{}, nil)
	g.variant("padding.id_mismatch", "padding", func(r *artifact.Record) {
		other := g.records["padding.rotated"]
		r.CapsuleID = other.CapsuleID
		r.ProducerEnvelope = signID(t, other.CapsuleID, g.keys["trusted"])
	})
	g.variant("padding.envelope_other_capsule", "padding", func(r *artifact.Record) { r.ProducerEnvelope = empty.Envelope })
	// A "__proto__" member, which a JavaScript decoder may not list among the
	// object's own members (an object value can become the prototype, a
	// primitive one can be dropped): every language must still refuse it.
	g.variant("padding.proto_member", "padding", func(r *artifact.Record) {
		r.Capsule = append([]byte(`{"__proto__":{"links":[]},`), r.Capsule[1:]...)
	})
	g.variant("padding.proto_string", "padding", func(r *artifact.Record) {
		r.Capsule = append([]byte(`{"__proto__":"x",`), r.Capsule[1:]...)
	})
	// Another valid nonce under the original capsule_id and envelope: only the
	// recomputed id catches it.
	g.variant("padding.content_tampered", "padding", func(r *artifact.Record) {
		r.Capsule = bytes.Replace(r.Capsule, []byte(strings.Repeat("11", 32)), []byte(strings.Repeat("99", 32)), 1)
	})

	// Codec-only synthetic records: opaque bytes, not a verifiable Capsule.
	g.add("synthetic", artifact.Record{CapsuleID: strings.Repeat("a", 64), Capsule: []byte("{}"), ProducerEnvelope: []byte{0, 1, 2, 255},
		Artifacts: []artifact.Artifact{
			{Name: "payload", Binding: artifact.PayloadDigest, Content: []byte("{}\n"), State: artifact.Present},
			{Name: "agent_output", Binding: artifact.AgentOutputDigest, State: artifact.NeverRetained},
		}})
	g.stored("synthetic")
	g.add("synthetic.empty", artifact.Record{CapsuleID: strings.Repeat("a", 64), Capsule: []byte("{}"), ProducerEnvelope: []byte{0, 1, 2, 255},
		Artifacts: []artifact.Artifact{}})

	v := Vectors{
		Format:   "artifact-store-conformance/v1",
		Contract: "docs/artifact-store.md",
		Keys:     map[string]string{},
		Records:  g.records,
	}
	for name, key := range g.keys {
		v.Keys[name] = hex.EncodeToString(key.Public().(ed25519.PublicKey))
	}
	for _, name := range []string{"synthetic.stored", "synthetic.purged", "synthetic.empty", "basic.stored", "basic.purged", "blank.stored", "empty.stored", "padding.stored"} {
		sum, err := g.records[name].StorageChecksum()
		require.NoError(t, err)
		v.Checksums = append(v.Checksums, ChecksumVector{Record: name, StorageChecksum: sum})
	}
	for _, name := range []string{"synthetic.stored", "synthetic.purged", "synthetic.empty", "basic.stored", "basic.purged", "blank.stored", "blank.purged", "rotated.stored", "padding.stored"} {
		line, err := jsonv2.Marshal(g.records[name])
		require.NoError(t, err)
		v.Lines = append(v.Lines, LineVector{Record: name, Line: string(line)})
	}
	v.Open = openVectors(v.Keys["trusted"])
	v.Cases = append(cases(g.records), indexCases(g.records)...)

	data, err := jsonv2.Marshal(v, jsonv2.Deterministic(true), jsontext.WithIndent("  "))
	require.NoError(t, err)
	return append(data, '\n')
}

// signEnvelope signs a Producer Envelope over sealed's Capsule ID with identity.
func signEnvelope(t *testing.T, sealed emit.Result, identity emit.SigningIdentity) []byte {
	built := emit.BuiltPayload{CapsuleID: sealed.CapsuleID, JSON: sealed.Payload}
	envelope, err := emit.Sign(built, identity)
	require.NoError(t, err)
	return envelope
}

// padding adds a padding record signed by key: the reserved members, then
// fields (which may override them), with its capsule_id recomputed over the
// result. Its bytes are compact JSON with sorted keys, the form a Rust ledger
// line takes without its local-only signature and key_id.
func (g *generator) padding(name, key string, fields map[string]any, artifacts []artifact.Artifact) {
	body := map[string]any{"record_type": artifact.PaddingRecordType, "epistemic_type": "producer_claim"}
	for member, value := range fields {
		body[member] = value
	}
	id, err := emit.DigestJSON(body)
	require.NoError(g.t, err)
	body["capsule_id"] = id
	capsule, err := jsonv2.Marshal(body, jsonv2.Deterministic(true))
	require.NoError(g.t, err)
	if artifacts == nil {
		artifacts = []artifact.Artifact{}
	}
	g.add(name, artifact.Record{CapsuleID: id, Capsule: capsule, ProducerEnvelope: signID(g.t, id, g.keys[key]), Artifacts: artifacts})
}

// signID signs a Producer Envelope over the raw Capsule ID id: the encoding
// emit.Sign uses, which only accepts a Capsule, so a padding record's envelope
// is built here and checked with emit.VerifyEnvelope.
func signID(t *testing.T, id string, key ed25519.PrivateKey) []byte {
	public := key.Public().(ed25519.PublicKey)
	protected := []byte{0xa3, 0x03, 0x78, byte(len(emit.ContentType))}
	protected = append(protected, emit.ContentType...)
	protected = append(protected, 0x04, 0x58, ed25519.PublicKeySize)
	protected = append(protected, public...)
	protected = append(protected, 0x01, 0x27)
	payload, err := hex.DecodeString(id)
	require.NoError(t, err)
	message := cose.NewSign1Message()
	message.Headers.RawProtected = append([]byte{0x58, byte(len(protected))}, protected...)
	message.Headers.Protected.SetAlgorithm(cose.AlgorithmEdDSA)
	message.Payload = payload
	signer, err := cose.NewSigner(cose.AlgorithmEdDSA, key)
	require.NoError(t, err)
	require.NoError(t, message.Sign(rand.Reader, nil, signer))
	envelope, err := message.MarshalCBOR()
	require.NoError(t, err)
	verified, err := emit.VerifyEnvelope(id, envelope)
	require.NoError(t, err)
	require.True(t, bytes.Equal(verified.PublicKey, public))
	return envelope
}

func openVectors(trusted string) []OpenVector {
	key := []string{trusted}
	return []OpenVector{
		{Name: "minimal_namespace", Namespace: "a", Trusted: key, Expect: "ok"},
		{Name: "punctuated_namespace", Namespace: "Tenant-1.prod_a", Trusted: key, Expect: "ok"},
		{Name: "longest_namespace", Namespace: strings.Repeat("n", 64), Trusted: key, Expect: "ok"},
		{Name: "empty_namespace", Namespace: "", Trusted: key, Expect: "invalid"},
		{Name: "leading_dash", Namespace: "-a", Trusted: key, Expect: "invalid"},
		{Name: "leading_dot", Namespace: ".a", Trusted: key, Expect: "invalid"},
		{Name: "namespace_too_long", Namespace: strings.Repeat("n", 65), Trusted: key, Expect: "invalid"},
		{Name: "namespace_slash", Namespace: "a/b", Trusted: key, Expect: "invalid"},
		{Name: "namespace_space", Namespace: "a b", Trusted: key, Expect: "invalid"},
		{Name: "namespace_trailing_newline", Namespace: "a\n", Trusted: key, Expect: "invalid"},
		{Name: "namespace_non_ascii", Namespace: "ä", Trusted: key, Expect: "invalid"},
		{Name: "no_trusted_keys", Namespace: "a", Trusted: []string{}, Expect: "invalid"},
		{Name: "short_key", Namespace: "a", Trusted: []string{trusted[:62]}, Expect: "invalid"},
		{Name: "long_key", Namespace: "a", Trusted: []string{trusted + "00"}, Expect: "invalid"},
	}
}

func put(record, expect string) Step { return Step{Op: "put", Record: record, Expect: expect} }
func get(id, expect, result string) Step {
	return Step{Op: "get", ID: &id, Expect: expect, Result: result}
}
func purge(id, expect string) Step { return Step{Op: "purge", ID: &id, Expect: expect} }

func initIndex(paths ...string) Step { return Step{Op: "init_index", Paths: paths, Expect: "ok"} }

// find expects records by name, in order, and whether a further page exists.
func find(where map[string]string, records ...string) Step {
	return Step{Op: "find", Where: where, Expect: "ok", Found: records}
}

func indexCases(r map[string]artifact.Record) []Case {
	main := map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted"}}}
	paths := []string{"operator", "developer", "fixture.tags", "fixture.level"}
	op := func(v string) map[string]string { return map[string]string{"operator": v} }
	tag := func(v string) map[string]string { return map[string]string{"fixture.tags": v} }
	since := func(at string, records ...string) Step { s := find(nil, records...); s.Since = at; return s }
	bad := func(s Step) Step { s.Expect, s.Found = "invalid", nil; return s }
	putABC := []Step{put("index.a", "ok"), put("index.b", "ok"), put("index.c", "ok")}
	index := []string{"index"}
	return []Case{
		{Name: "index_find_equality", Description: "Find returns the records whose declared paths hold every given value, in admission order.",
			Requires: index, Stores: main,
			Steps: append(append([]Step{initIndex(paths...)}, putABC...),
				find(op("op-a"), "index.a", "index.c"), find(op("op-b"), "index.b"), find(op("nobody")),
				find(tag("red"), "index.a", "index.b"), find(tag("blue"), "index.a", "index.c"),
				find(map[string]string{"operator": "op-a", "fixture.tags": "red"}, "index.a"),
				find(map[string]string{"operator": "op-a", "developer": "dev-2", "fixture.tags": "blue"}, "index.c"),
				find(map[string]string{"fixture.level": "one"}, "index.b"), find(map[string]string{"fixture.level": "1"}),
				find(tag("7")), find(nil, "index.a", "index.b", "index.c"))},
		{Name: "index_find_since", Description: "Since is an inclusive lower bound on the top-level timestamp, compared in UTC; records without one never match it.",
			Requires: index, Stores: main,
			Steps: append(append([]Step{initIndex(paths...), put("padding", "ok")}, putABC...),
				find(nil, "padding", "index.a", "index.b", "index.c"),
				since("2026-10-16T00:00:00Z", "index.b", "index.c"), since("2026-10-16T02:00:00+02:00", "index.b", "index.c"),
				since("2026-10-15T19:00:00.5-05:00", "index.c"), since("2026-10-15T19:00:00-05:00", "index.b", "index.c"),
				since("2026-10-17T00:00:00.000001Z", "index.c"), since("2026-10-17T00:00:00.0000019Z", "index.c"),
				since("2026-10-17T00:00:00.000002Z"), since("2000-01-01T00:00:00Z", "index.a", "index.b", "index.c"),
				func() Step {
					s := since("2026-10-16T00:00:00Z", "index.a")
					s.Where = op("op-a")
					s.Found = []string{"index.c"}
					return s
				}())},
		{Name: "index_find_paging", Description: "Find pages by limit; the cursor continues after the last returned record.",
			Requires: index, Stores: main,
			Steps: append(append([]Step{initIndex(paths...)}, putABC...),
				Step{Op: "find", Limit: 1, Expect: "ok", Found: []string{"index.a"}, More: true},
				Step{Op: "find", Limit: 1, Continue: true, Expect: "ok", Found: []string{"index.b"}, More: true},
				Step{Op: "find", Limit: 1, Continue: true, Expect: "ok", Found: []string{"index.c"}},
				Step{Op: "find", Where: op("op-a"), Limit: 1, Expect: "ok", Found: []string{"index.a"}, More: true},
				Step{Op: "find", Where: op("op-a"), Limit: 1, Continue: true, Expect: "ok", Found: []string{"index.c"}},
				Step{Op: "find", Where: op("op-a"), Limit: 2, Expect: "ok", Found: []string{"index.a", "index.c"}})},
		{Name: "index_backfill", Description: "Declaring paths indexes the records already stored; retries and purges change nothing.",
			Requires: index, Stores: main,
			Steps: []Step{put("index.a", "ok"), put("index.b", "ok"), put("basic", "ok"), initIndex(paths...),
				{Op: "find", Where: tag("red"), Unordered: true, Expect: "ok", Found: []string{"index.a", "index.b"}},
				put("index.c", "ok"), put("index.a", "ok"), purge(r["basic"].CapsuleID, "ok"),
				{Op: "find", Unordered: true, Expect: "ok", Found: []string{"index.a", "index.b", "basic", "index.c"}},
				find(op("op-a"), "index.a", "index.c")}},
		{Name: "index_declaration", Description: "The declared paths are a set: the same set again is a no-op, another is a conflict, and reindex replaces it.",
			Requires: index, Stores: main,
			Steps: append(append([]Step{initIndex("operator", "fixture.tags")}, putABC...),
				initIndex("fixture.tags", "operator"),
				Step{Op: "init_index", Paths: []string{"operator"}, Expect: "conflict"},
				Step{Op: "init_index", Paths: []string{"operator", "fixture.tags", "developer"}, Expect: "conflict"},
				bad(find(map[string]string{"developer": "dev-1"})),
				Step{Op: "reindex", Paths: []string{"developer"}, Expect: "ok"},
				Step{Op: "find", Where: map[string]string{"developer": "dev-1"}, Unordered: true, Expect: "ok", Found: []string{"index.a", "index.b"}},
				bad(find(op("op-a"))),
				Step{Op: "reindex", Paths: []string{}, Expect: "ok"},
				Step{Op: "find", Unordered: true, Expect: "ok", Found: []string{"index.a", "index.b", "index.c"}},
				bad(find(map[string]string{"developer": "dev-1"})))},
		{Name: "index_invalid", Description: "Malformed paths, queries and cursors are invalid, and so is find before any declaration.",
			Requires: index, Stores: main,
			Steps: []Step{bad(find(nil)), put("index.a", "ok"),
				{Op: "init_index", Paths: []string{""}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{"a..b"}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{".a"}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{"a b"}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{"a.b.c.d.e.f.g.h.i"}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{strings.Repeat("s", 65)}, Expect: "invalid"},
				{Op: "init_index", Paths: []string{"operator", "operator"}, Expect: "invalid"},
				{Op: "init_index", Paths: manyPaths(33), Expect: "invalid"},
				{Op: "reindex", Paths: []string{"ä"}, Expect: "invalid"},
				bad(find(nil)),
				initIndex(manyPaths(32)...), {Op: "init_index", Paths: append(manyPaths(32)[1:], "p32"), Expect: "conflict"},
				{Op: "reindex", Paths: []string{"a.b.c.d.e.f.g.h", "operator"}, Expect: "ok"},
				bad(find(map[string]string{"developer": "dev-1"})),
				find(op("op-a"), "index.a"),
				bad(since("2026-10-16")), bad(since("2026-10-16 00:00:00Z")), bad(since("2026-10-16t00:00:00z")),
				bad(since("2026-02-30T00:00:00Z")), bad(since("2026-10-16T24:00:00Z")), bad(since("2026-10-16T00:00:60Z")),
				bad(since("2026-10-16T00:00:00.Z")), bad(since("2026-10-16T00:00:00.0000000001Z")), bad(since("2026-10-16T00:00:00+24:00")),
				bad(since("２０２６-10-16T00:00:00Z")),
				{Op: "find", Limit: 1001, Expect: "invalid"}, {Op: "find", Limit: -1, Expect: "invalid"},
				{Op: "find", After: "x", Expect: "invalid"}, {Op: "find", After: "0", Expect: "invalid"},
				{Op: "find", After: "01", Expect: "invalid"}, {Op: "find", After: "-1", Expect: "invalid"},
				{Op: "find", Limit: 1000, Expect: "ok", Found: []string{"index.a"}}}},
		{Name: "index_durable", Description: "A stored declaration survives reopening, and a store that never declared paths still indexes its writes.",
			Requires: []string{"index", "durable_index"}, Stores: main,
			Steps: []Step{initIndex(paths...), put("index.a", "ok"), {Op: "reopen", Expect: "ok"},
				put("index.b", "ok"), find(tag("red"), "index.a", "index.b"),
				{Op: "init_index", Paths: []string{"operator"}, Expect: "conflict"}, {Op: "reopen", Expect: "ok"},
				initIndex(paths...), put("index.c", "ok"), find(op("op-a"), "index.a", "index.c")}},
		{Name: "index_namespace_isolation", Description: "Namespaces sharing storage keep their own declarations and indexes.",
			Requires: []string{"index", "durable_index", "shared_backing"},
			Stores: map[string]StoreSpec{"main": {Namespace: "alpha", Trusted: []string{"trusted"}},
				"beta": {Namespace: "beta", Trusted: []string{"trusted"}}},
			Steps: []Step{initIndex("operator"), {Op: "init_index", Store: "beta", Paths: []string{"developer"}, Expect: "ok"},
				put("index.a", "ok"), {Op: "put", Store: "beta", Record: "index.b", Expect: "ok"},
				find(nil, "index.a"), {Op: "find", Store: "beta", Expect: "ok", Found: []string{"index.b"}},
				bad(find(map[string]string{"developer": "dev-1"})),
				{Op: "find", Store: "beta", Where: map[string]string{"developer": "dev-1"}, Expect: "ok", Found: []string{"index.b"}}}},
	}
}

func manyPaths(n int) []string {
	paths := make([]string, n)
	for i := range paths {
		paths[i] = fmt.Sprintf("p%02d", i)
	}
	return paths
}

func cases(r map[string]artifact.Record) []Case {
	main := map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted"}}}
	basic, empty, blank, effect := r["basic"].CapsuleID, r["empty"].CapsuleID, r["blank"].CapsuleID, r["effect"].CapsuleID
	padding, paddingNote := r["padding"].CapsuleID, r["padding.with_note"].CapsuleID
	rotated, untrusted := r["rotated"].CapsuleID, r["untrusted"].CapsuleID
	rejections := []Step{}
	for _, c := range []struct{ record, code string }{
		{"basic.untrusted_envelope", "untrusted_signer"},
		{"untrusted", "untrusted_signer"},
		{"basic.envelope_other_capsule", "invalid"},
		{"basic.wrong_id", "invalid"},
		{"basic.uppercase_id", "invalid"},
		{"basic.capsule_tampered", "invalid"},
		{"basic.payload_mismatch", "digest_mismatch"},
		{"basic.payload_not_json", "digest_mismatch"},
		{"effect.response_mismatch", "digest_mismatch"},
		{"basic.sha_mismatch", "corrupt"},
		{"basic.input_purged", "purged"},
		{"basic.corrupt_then_purged", "corrupt"},
		{"basic.purged_then_corrupt", "purged"},
		{"basic.too_many", "invalid"},
		{"basic.duplicate_name", "invalid"},
		{"basic.bad_name", "invalid"},
		{"basic.never_retained_with_content", "invalid"},
		{"basic.present_without_content", "invalid"},
		{"basic.unsupported_binding", "invalid"},
		{"basic.binding_absent_in_capsule", "invalid"},
		{"basic.unknown_state", "invalid"},
	} {
		rejections = append(rejections, put(c.record, c.code))
	}
	rejections = append(rejections,
		get(basic, "not_found", ""), get(empty, "not_found", ""), get(effect, "not_found", ""), get(untrusted, "not_found", ""))
	return []Case{
		{Name: "put_get_roundtrip", Description: "A stored record reads back with content_sha256 filled in.", Stores: main,
			Steps: []Step{put("basic", "ok"), get(basic, "ok", "basic.stored")}},
		{Name: "artifact_limit", Description: "Exactly 64 artifacts are admitted; 65 are invalid.", Stores: main,
			Steps: []Step{put("basic.too_many", "invalid"), put("basic.most_artifacts", "ok"), get(basic, "ok", "basic.most_artifacts.stored")}},
		{Name: "idempotent_retry", Description: "Byte-identical retries, in any artifact order, succeed without change.", Stores: main,
			Steps: []Step{put("basic", "ok"), put("basic", "ok"), put("basic.reordered", "ok"), get(basic, "ok", "basic.stored")}},
		{Name: "conflicting_rewrite", Description: "A different record under the same Capsule ID is a conflict and changes nothing.", Stores: main,
			Steps: []Step{put("basic", "ok"), put("basic.conflict_note", "conflict"), put("basic.conflict_extra", "conflict"),
				put("basic.conflict_missing", "conflict"), put("basic.conflict_unbound", "conflict"),
				put("basic.output_never_retained", "conflict"), get(basic, "ok", "basic.stored")}},
		{Name: "rejected_records", Description: "Each record failing admission is rejected with its code and nothing is stored.", Stores: main,
			Steps: rejections},
		{Name: "unknown_ids", Description: "Well-formed unknown IDs are not_found.", Stores: main,
			Steps: []Step{get(basic, "not_found", ""), purge(basic, "not_found"), put("empty", "ok"), get(basic, "not_found", ""), purge(basic, "not_found")}},
		{Name: "malformed_ids", Description: "Malformed IDs are invalid before any lookup.", Stores: main,
			Steps: []Step{
				get(strings.ToUpper(basic), "invalid", ""), get(basic[:63], "invalid", ""), get(basic+"0", "invalid", ""),
				get("", "invalid", ""), get(basic+"\n", "invalid", ""), get(strings.Repeat("g", 64), "invalid", ""),
				purge(strings.ToUpper(basic), "invalid"), purge("", "invalid"), purge(basic+"\n", "invalid"),
			}},
		{Name: "purge_lifecycle", Description: "Purge tombstones present originals idempotently; purged originals cannot be rewritten.", Stores: main,
			Steps: []Step{put("basic", "ok"), purge(basic, "ok"), get(basic, "ok", "basic.purged"), purge(basic, "ok"),
				get(basic, "ok", "basic.purged"), put("basic", "purged"), put("basic.reordered", "purged"),
				put("basic.conflict_note", "conflict"), get(basic, "ok", "basic.purged")}},
		{Name: "purge_is_per_record", Description: "Purging one record leaves the others intact.", Stores: main,
			Steps: []Step{put("basic", "ok"), put("effect", "ok"), purge(basic, "ok"), get(effect, "ok", "effect.stored"),
				get(basic, "ok", "basic.purged")}},
		{Name: "never_retained_survives_purge", Description: "Purge leaves never_retained artifacts unchanged.", Stores: main,
			Steps: []Step{put("basic.output_never_retained", "ok"), get(basic, "ok", "basic.output_never_retained.stored"),
				purge(basic, "ok"), get(basic, "ok", "basic.output_never_retained.purged")}},
		{Name: "empty_inventory", Description: "A record with no artifacts stores, purges and retries.", Stores: main,
			Steps: []Step{put("empty", "ok"), get(empty, "ok", "empty.stored"), purge(empty, "ok"), get(empty, "ok", "empty.stored"),
				put("empty", "ok")}},
		{Name: "zero_length_original", Description: "Zero-length content is present, distinct from absent content.", Stores: main,
			Steps: []Step{put("blank", "ok"), get(blank, "ok", "blank.stored"), purge(blank, "ok"), get(blank, "ok", "blank.purged")}},
		{Name: "effect_bindings", Description: "Effect request and response preimages verify against their digests.", Stores: main,
			Steps: []Step{put("effect", "ok"), get(effect, "ok", "effect.stored"), purge(effect, "ok"), get(effect, "ok", "effect.purged")}},
		{Name: "key_rotation", Description: "A store trusting several keys admits records from each.",
			Stores: map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted", "rotated"}}},
			Steps:  []Step{put("rotated", "ok"), put("basic", "ok"), get(rotated, "ok", "rotated.stored"), get(basic, "ok", "basic.stored")}},
		{Name: "bare_record", Description: "A record without originals stores, retries idempotently and conflicts with another envelope.",
			Stores: map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted", "rotated"}}},
			Steps: []Step{put("empty", "ok"), get(empty, "ok", "empty.stored"), put("empty", "ok"),
				put("empty.rotated_envelope", "conflict"), get(empty, "ok", "empty.stored")}},
		{Name: "bare_rejections", Description: "Records without originals that fail admission are rejected and nothing is stored.", Stores: main,
			Steps: []Step{put("empty.untrusted_envelope", "untrusted_signer"), put("empty.envelope_other_capsule", "invalid"),
				put("empty.wrong_id", "invalid"), put("empty.capsule_tampered", "invalid"),
				get(empty, "not_found", ""), get(basic, "not_found", "")}},
		{Name: "padding_record", Description: "A padding record is admitted by its closed shape, reads back and retries idempotently.",
			Stores: map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted", "rotated"}}},
			Steps: []Step{put("padding", "ok"), get(padding, "ok", "padding.stored"), put("padding", "ok"),
				put("padding.rotated", "ok"), put("empty", "ok"), get(padding, "ok", "padding.stored")}},
		{Name: "padding_rejections", Description: "Padding records failing their shape, id, envelope or attachment rules are rejected.", Stores: main,
			Steps: []Step{put("padding.untrusted", "untrusted_signer"), put("padding.capsule_profile", "invalid"),
				put("padding.extra_member", "invalid"), put("padding.epistemic_type", "invalid"),
				put("padding.nonce_uppercase", "invalid"), put("padding.nonce_missing", "invalid"),
				put("padding.id_mismatch", "invalid"), put("padding.envelope_other_capsule", "invalid"),
				put("padding.content_tampered", "invalid"), put("padding.proto_member", "invalid"),
				put("padding.proto_string", "invalid"),
				get(padding, "not_found", "")}},
		{Name: "padding_unbound_original", Description: "A padding record may keep an unbound attachment, which purges like any original, but no bound one.", Stores: main,
			Steps: []Step{put("padding.bound", "invalid"), put("padding.with_note", "ok"), get(paddingNote, "ok", "padding.with_note.stored"),
				purge(paddingNote, "ok"), get(paddingNote, "ok", "padding.with_note.purged")}},
		{Name: "durable_reopen", Description: "Records, purges and conflicts persist across reopening.", Requires: []string{"durable"}, Stores: main,
			Steps: []Step{put("basic", "ok"), put("empty", "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.stored"),
				put("basic.conflict_note", "conflict"), purge(basic, "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.purged"),
				put("basic", "purged"), get(empty, "ok", "empty.stored")}},
		{Name: "untrusted_on_read", Description: "Reads fail closed when the signer is no longer trusted; purge still erases.",
			Requires: []string{"durable"}, Stores: main,
			Steps: []Step{put("basic", "ok"), {Op: "reopen", Trusted: []string{"rotated"}, Expect: "ok"}, get(basic, "untrusted_signer", ""),
				purge(basic, "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.purged")}},
		{Name: "bare_untrusted_on_read", Description: "Reads of a record without originals fail closed when its signer is no longer trusted.",
			Requires: []string{"durable"}, Stores: main,
			Steps: []Step{put("empty", "ok"), {Op: "reopen", Trusted: []string{"rotated"}, Expect: "ok"}, get(empty, "untrusted_signer", ""),
				{Op: "reopen", Expect: "ok"}, get(empty, "ok", "empty.stored")}},
		{Name: "namespace_isolation", Description: "Namespaces sharing storage do not see each other's records.",
			Requires: []string{"shared_backing"},
			Stores: map[string]StoreSpec{"main": {Namespace: "alpha", Trusted: []string{"trusted"}},
				"beta": {Namespace: "beta", Trusted: []string{"trusted"}}},
			Steps: []Step{put("basic", "ok"), {Op: "get", Store: "beta", ID: &basic, Expect: "not_found"},
				{Op: "purge", Store: "beta", ID: &basic, Expect: "not_found"},
				{Op: "put", Store: "beta", Record: "basic.conflict_note", Expect: "ok"},
				{Op: "purge", Store: "beta", ID: &basic, Expect: "ok"}, get(basic, "ok", "basic.stored")}},
	}
}
