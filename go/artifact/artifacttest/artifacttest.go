// Package artifacttest runs the shared, language-neutral artifact store
// conformance suite (test-vectors/artifact-store, specified by
// docs/artifact-store.md) against any artifact.Store, built in or user-supplied.
//
//	func TestConformance(t *testing.T) {
//		artifacttest.Run(t, artifacttest.Harness[string]{
//			NewBacking: func(t *testing.T) string { return filepath.Join(t.TempDir(), "a.jsonl") },
//			Open: func(t *testing.T, path, ns string, keys []ed25519.PublicKey) (artifact.Store, error) {
//				return jsonl.New(path, ns, keys)
//			},
//			Capabilities: artifacttest.Capabilities{Durable: true},
//		})
//	}
package artifacttest

import (
	"bytes"
	"crypto/ed25519"
	_ "embed"
	"encoding/hex"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"slices"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
)

// vectorsJSON is a byte-identical copy of test-vectors/artifact-store/vectors.json;
// the package tests fail if the two drift.
//
//go:embed vectors.json
var vectorsJSON []byte

// Capabilities declares the optional behaviors a backend supports. Cases that
// require an undeclared capability are skipped.
type Capabilities struct {
	// Durable: a store reopened over the same backing sees earlier writes.
	Durable bool
	// SharedBacking: stores with different namespaces can share one backing.
	SharedBacking bool
	// DurableIndex: a store reopened over the same backing keeps the index
	// paths declared earlier (the SQL backends). The "index" capability is
	// detected: the store implements artifact.IndexStore.
	DurableIndex bool
}

// Harness adapts one backend to the suite. B is the backend's storage handle
// (a file path, a *sql.DB, ...).
type Harness[B any] struct {
	// NewBacking returns fresh, empty storage for one case.
	NewBacking func(t *testing.T) B
	// Open constructs a store over backing without calling Init. It returns the
	// constructor's error so the suite can check configuration validation.
	Open func(t *testing.T, backing B, namespace string, trusted []ed25519.PublicKey) (artifact.Store, error)
	Capabilities
}

// Vectors is the decoded conformance file. See test-vectors/artifact-store/README.md.
type Vectors struct {
	Format    string                     `json:"format"`
	Contract  string                     `json:"contract"`
	Keys      map[string]string          `json:"keys"`
	Records   map[string]artifact.Record `json:"records"`
	Checksums []ChecksumVector           `json:"checksums"`
	Lines     []LineVector               `json:"lines"`
	IndexRows []IndexRowVector           `json:"index_rows"`
	Open      []OpenVector               `json:"open"`
	Cases     []Case                     `json:"cases"`
}

// ChecksumVector pins Record.StorageChecksum for a named record.
type ChecksumVector struct {
	Record          string `json:"record"`
	StorageChecksum string `json:"storage_checksum"`
}

// LineVector pins the exact JSONL line for a named record.
type LineVector struct {
	Record string `json:"record"`
	Line   string `json:"line"`
}

// IndexRowVector pins what the index extension stores for a named record
// under declared Paths, so SQL index rows written by one language are found by
// the others: the stored declaration, the record's time (UTC microseconds in
// decimal, empty without one) and each entry's path and value key.
type IndexRowVector struct {
	Record  string             `json:"record"`
	Paths   []string           `json:"paths"`
	Stored  string             `json:"stored"`
	TS      string             `json:"ts"`
	Entries []IndexEntryVector `json:"entries"`
}

// IndexEntryVector is one index row: a path and the SHA-256 of its value.
type IndexEntryVector struct {
	Path        string `json:"path"`
	ValueSHA256 string `json:"value_sha256"`
}

// OpenVector checks constructor validation. Trusted holds raw hex keys.
type OpenVector struct {
	Name      string   `json:"name"`
	Namespace string   `json:"namespace"`
	Trusted   []string `json:"trusted"`
	Expect    string   `json:"expect"`
}

// StoreSpec names one store a case opens over the case's backing.
type StoreSpec struct {
	Namespace string   `json:"namespace"`
	Trusted   []string `json:"trusted"`
}

// Case is one operation sequence over fresh backing.
type Case struct {
	Name        string               `json:"name"`
	Description string               `json:"description"`
	Requires    []string             `json:"requires,omitempty"`
	Stores      map[string]StoreSpec `json:"stores"`
	Steps       []Step               `json:"steps"`
}

// Step is one operation and its expected outcome: "ok" or an error code.
type Step struct {
	Op      string   `json:"op"`
	Store   string   `json:"store,omitempty"`
	Record  string   `json:"record,omitempty"`
	ID      *string  `json:"id,omitzero"`
	Trusted []string `json:"trusted,omitempty"`
	Expect  string   `json:"expect"`
	Result  string   `json:"result,omitempty"`
	// Index extension steps: init_index and reindex take Paths; find takes
	// Where, Since, After (a literal cursor), Limit and Continue (use the
	// previous find's Next), and expects Found (record names, in order unless
	// Unordered) and More (a non-empty Next).
	Paths     []string          `json:"paths,omitzero"`
	Where     map[string]string `json:"where,omitempty"`
	Since     string            `json:"since,omitempty"`
	After     string            `json:"after,omitempty"`
	Limit     int               `json:"limit,omitzero"`
	Continue  bool              `json:"continue,omitzero"`
	Found     []string          `json:"found,omitempty"`
	More      bool              `json:"more,omitzero"`
	Unordered bool              `json:"unordered,omitzero"`
}

// Load decodes the embedded conformance vectors.
func Load() (Vectors, error) {
	var v Vectors
	if err := jsonv2.Unmarshal(vectorsJSON, &v); err != nil {
		return Vectors{}, err
	}
	return v, nil
}

// Codes maps each contract error code to its Go sentinel.
var Codes = map[string]error{
	"invalid":          artifact.ErrInvalid,
	"conflict":         artifact.ErrConflict,
	"not_found":        artifact.ErrNotFound,
	"purged":           artifact.ErrPurged,
	"digest_mismatch":  artifact.ErrDigestMismatch,
	"untrusted_signer": artifact.ErrUntrustedSigner,
	"corrupt":          artifact.ErrCorrupt,
}

// Run executes every constructor vector and case against the harness.
func Run[B any](t *testing.T, h Harness[B]) {
	t.Helper()
	v, err := Load()
	if err != nil {
		t.Fatalf("load vectors: %v", err)
	}
	for _, o := range v.Open {
		t.Run("open/"+o.Name, func(t *testing.T) {
			keys := make([]ed25519.PublicKey, 0, len(o.Trusted))
			for _, k := range o.Trusted {
				raw, err := hex.DecodeString(k)
				if err != nil {
					t.Fatalf("vector key %q: %v", k, err)
				}
				keys = append(keys, raw)
			}
			store, err := h.Open(t, h.NewBacking(t), o.Namespace, keys)
			checkOutcome(t, "open", o.Expect, err)
			if err == nil && store.Namespace() != o.Namespace {
				t.Fatalf("Namespace() = %q, want %q", store.Namespace(), o.Namespace)
			}
		})
	}
	for _, c := range v.Cases {
		t.Run(c.Name, func(t *testing.T) {
			for _, r := range c.Requires {
				if (r == "durable" && !h.Durable) || (r == "shared_backing" && !h.SharedBacking) ||
					(r == "durable_index" && !h.DurableIndex) || (r == "index" && !indexes(t, h)) {
					t.Skipf("backend lacks capability %s", r)
				}
			}
			runCase(t, h, v, c)
		})
	}
}

// indexes reports whether the backend's stores implement artifact.IndexStore.
func indexes[B any](t *testing.T, h Harness[B]) bool {
	store, err := h.Open(t, h.NewBacking(t), "probe", namedKeys(t, mustLoad(t), []string{"trusted"}))
	if err != nil {
		t.Fatalf("open probe store: %v", err)
	}
	_, ok := store.(artifact.IndexStore)
	return ok
}

func mustLoad(t *testing.T) Vectors {
	v, err := Load()
	if err != nil {
		t.Fatalf("load vectors: %v", err)
	}
	return v
}

func runCase[B any](t *testing.T, h Harness[B], v Vectors, c Case) {
	backing := h.NewBacking(t)
	stores := map[string]artifact.Store{}
	next := ""
	open := func(name string, trusted []string) {
		spec := c.Stores[name]
		store, err := h.Open(t, backing, spec.Namespace, namedKeys(t, v, trusted))
		if err != nil {
			t.Fatalf("open store %s: %v", name, err)
		}
		for range 2 { // Init is idempotent.
			if err := store.Init(t.Context()); err != nil {
				t.Fatalf("init store %s: %v", name, err)
			}
		}
		if store.Namespace() != spec.Namespace {
			t.Fatalf("Namespace() = %q, want %q", store.Namespace(), spec.Namespace)
		}
		stores[name] = store
	}
	names := make([]string, 0, len(c.Stores))
	for name := range c.Stores {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		open(name, c.Stores[name].Trusted)
	}
	for i, s := range c.Steps {
		name := s.Store
		if name == "" {
			name = "main"
		}
		id := ""
		if s.ID != nil {
			id = *s.ID
		}
		label := fmt.Sprintf("step %d (%s %s%s)", i, s.Op, s.Record, id)
		store := stores[name]
		switch s.Op {
		case "put":
			record, ok := v.Records[s.Record]
			if !ok {
				t.Fatalf("%s: unknown record %q", label, s.Record)
			}
			checkOutcome(t, label, s.Expect, store.Put(t.Context(), record))
		case "get":
			got, err := store.Get(t.Context(), id)
			checkOutcome(t, label, s.Expect, err)
			if err == nil {
				want, ok := v.Records[s.Result]
				if !ok {
					t.Fatalf("%s: unknown result %q", label, s.Result)
				}
				if diff := Diff(want, got); diff != "" {
					t.Fatalf("%s: returned record differs from %s: %s", label, s.Result, diff)
				}
			}
		case "purge":
			checkOutcome(t, label, s.Expect, store.Purge(t.Context(), id))
		case "init_index", "reindex":
			is := store.(artifact.IndexStore)
			run := is.InitIndex
			if s.Op == "reindex" {
				run = is.Reindex
			}
			checkOutcome(t, label, s.Expect, run(t.Context(), s.Paths))
		case "find":
			q := artifact.Query{Where: s.Where, Since: s.Since, After: s.After, Limit: s.Limit}
			if s.Continue {
				q.After = next
			}
			page, err := store.(artifact.IndexStore).Find(t.Context(), q)
			checkOutcome(t, label, s.Expect, err)
			if err == nil {
				checkFound(t, label, v, s, page)
				next = page.Next
			}
		case "reopen":
			trusted := s.Trusted
			if trusted == nil {
				trusted = c.Stores[name].Trusted
			}
			open(name, trusted)
		default:
			t.Fatalf("%s: unknown op", label)
		}
	}
}

func checkFound(t *testing.T, label string, v Vectors, s Step, page artifact.Page) {
	t.Helper()
	want := make([]string, 0, len(s.Found))
	for _, name := range s.Found {
		record, ok := v.Records[name]
		if !ok {
			t.Fatalf("%s: unknown record %q", label, name)
		}
		want = append(want, record.CapsuleID)
	}
	got := slices.Clone(page.IDs)
	if s.Unordered {
		slices.Sort(want)
		slices.Sort(got)
	}
	if !slices.Equal(got, want) {
		t.Fatalf("%s: found %v, want %v (%v)", label, got, want, s.Found)
	}
	if (page.Next != "") != s.More {
		t.Fatalf("%s: next %q, want more=%t", label, page.Next, s.More)
	}
}

func namedKeys(t *testing.T, v Vectors, names []string) []ed25519.PublicKey {
	keys := make([]ed25519.PublicKey, 0, len(names))
	for _, name := range names {
		raw, err := hex.DecodeString(v.Keys[name])
		if err != nil || len(raw) != ed25519.PublicKeySize {
			t.Fatalf("vector key %q is not a 32-byte hex key", name)
		}
		keys = append(keys, raw)
	}
	return keys
}

func checkOutcome(t *testing.T, label, expect string, err error) {
	t.Helper()
	if expect == "ok" {
		if err != nil {
			t.Fatalf("%s: want ok, got %v", label, err)
		}
		return
	}
	sentinel, known := Codes[expect]
	if !known {
		t.Fatalf("%s: unknown expected code %q", label, expect)
	}
	if !errors.Is(err, sentinel) {
		t.Fatalf("%s: want %s, got %v", label, expect, err)
	}
}

// Diff compares two records as the contract defines equality: artifact order is
// ignored, and absent content differs from zero-length content. It returns ""
// when they are equal.
func Diff(want, got artifact.Record) string {
	if want.CapsuleID != got.CapsuleID {
		return fmt.Sprintf("capsule_id %q != %q", got.CapsuleID, want.CapsuleID)
	}
	if !bytes.Equal(want.Capsule, got.Capsule) {
		return "capsule bytes differ"
	}
	if !bytes.Equal(want.ProducerEnvelope, got.ProducerEnvelope) {
		return "producer_envelope bytes differ"
	}
	if len(want.Artifacts) != len(got.Artifacts) {
		return fmt.Sprintf("%d artifacts, want %d", len(got.Artifacts), len(want.Artifacts))
	}
	byName := map[string]artifact.Artifact{}
	for _, a := range got.Artifacts {
		byName[a.Name] = a
	}
	for _, w := range want.Artifacts {
		g, ok := byName[w.Name]
		switch {
		case !ok:
			return "missing artifact " + w.Name
		case g.Binding != w.Binding:
			return fmt.Sprintf("%s binding %q, want %q", w.Name, g.Binding, w.Binding)
		case g.State != w.State:
			return fmt.Sprintf("%s state %q, want %q", w.Name, g.State, w.State)
		case g.ContentSHA256 != w.ContentSHA256:
			return fmt.Sprintf("%s content_sha256 %q, want %q", w.Name, g.ContentSHA256, w.ContentSHA256)
		case (g.Content == nil) != (w.Content == nil):
			return fmt.Sprintf("%s content presence %t, want %t", w.Name, g.Content != nil, w.Content != nil)
		case !bytes.Equal(g.Content, w.Content):
			return w.Name + " content differs"
		}
	}
	return ""
}
