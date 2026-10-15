package artifacttest

import (
	"bytes"
	"crypto/ed25519"
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
	input.Operator, input.Developer = "conformance-operator", "conformance-developer"
	input.Timestamp = time.Date(2026, 10, 15, 0, 0, 0, 0, time.UTC)
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
	for _, name := range []string{"synthetic.stored", "synthetic.purged", "synthetic.empty", "basic.stored", "basic.purged", "blank.stored", "empty.stored"} {
		sum, err := g.records[name].StorageChecksum()
		require.NoError(t, err)
		v.Checksums = append(v.Checksums, ChecksumVector{Record: name, StorageChecksum: sum})
	}
	for _, name := range []string{"synthetic.stored", "synthetic.purged", "synthetic.empty", "basic.stored", "basic.purged", "blank.stored", "blank.purged", "rotated.stored"} {
		line, err := jsonv2.Marshal(g.records[name])
		require.NoError(t, err)
		v.Lines = append(v.Lines, LineVector{Record: name, Line: string(line)})
	}
	v.Open = openVectors(v.Keys["trusted"])
	v.Cases = cases(g.records)

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

func cases(r map[string]artifact.Record) []Case {
	main := map[string]StoreSpec{"main": {Namespace: "conformance", Trusted: []string{"trusted"}}}
	basic, empty, blank, effect := r["basic"].CapsuleID, r["empty"].CapsuleID, r["blank"].CapsuleID, r["effect"].CapsuleID
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
		{Name: "durable_reopen", Description: "Records, purges and conflicts persist across reopening.", Requires: []string{"durable"}, Stores: main,
			Steps: []Step{put("basic", "ok"), put("empty", "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.stored"),
				put("basic.conflict_note", "conflict"), purge(basic, "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.purged"),
				put("basic", "purged"), get(empty, "ok", "empty.stored")}},
		{Name: "untrusted_on_read", Description: "Reads fail closed when the signer is no longer trusted; purge still erases.",
			Requires: []string{"durable"}, Stores: main,
			Steps: []Step{put("basic", "ok"), {Op: "reopen", Trusted: []string{"rotated"}, Expect: "ok"}, get(basic, "untrusted_signer", ""),
				purge(basic, "ok"), {Op: "reopen", Expect: "ok"}, get(basic, "ok", "basic.purged")}},
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
