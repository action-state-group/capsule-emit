// Package artifact persists exact sealed Capsules, Producer Envelopes, and
// associated business originals. It neither seals nor appends to a CLL.
package artifact

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	emit "github.com/action-state-group/capsule-emit-go"
)

// DigestField is a supported AAC format-4 JSON-DIGEST location, not a free-form
// JSON pointer. Payload is the application name; the current wire field is
// model_attestation.compute_attestation.agent_input_digest (emit-go/model.go).
type DigestField string

const (
	PayloadDigest        DigestField = "model_attestation.compute_attestation.agent_input_digest"
	AgentOutputDigest    DigestField = "model_attestation.compute_attestation.agent_output_digest"
	EffectRequestDigest  DigestField = "effect.request_digest"
	EffectResponseDigest DigestField = "effect.response_digest"
)

// RetentionState describes availability, not authenticity. Purged originals
// cannot be resurrected by retrying Put. NeverRetained must be explicit.
type RetentionState string

const (
	Present       RetentionState = "present"
	Purged        RetentionState = "purged"
	NeverRetained RetentionState = "never_retained"
)

var (
	ErrInvalid         = errors.New("invalid artifact record")
	ErrConflict        = errors.New("immutable record conflict")
	ErrNotFound        = errors.New("capsule not found")
	ErrPurged          = errors.New("originals were purged")
	ErrDigestMismatch  = errors.New("original does not match capsule digest")
	ErrUntrustedSigner = errors.New("producer signer is not trusted")
	ErrCorrupt         = errors.New("stored record integrity failure")
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var idPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Artifact holds an exact original, normally named payload or agent_output.
// Additional names support effect preimages and application attachments.
// Empty Binding explicitly means NOT authenticated by a Capsule digest.
// ContentSHA256 protects exact-byte storage integrity; it is not a signature
// claim and is different from the JCS digest referenced by Binding.
type Artifact struct {
	Name          string         `json:"name"`
	Binding       DigestField    `json:"binding,omitempty"`
	Content       []byte         `json:"content,omitempty"`
	State         RetentionState `json:"state"`
	ContentSHA256 string         `json:"content_sha256,omitempty"`
}

// Record groups a Capsule with one Producer Envelope and its originals.
// Content is copied, never resealed or reserialized. Records are immutable
// except for explicit purging of originals. An omitted artifact is undeclared,
// not evidence that it was never retained.
type Record struct {
	CapsuleID        string     `json:"capsule_id"`
	Capsule          []byte     `json:"capsule"`
	ProducerEnvelope []byte     `json:"producer_envelope"`
	Artifacts        []Artifact `json:"artifacts"`
}

// ArtifactVerification separates a verified preimage from an unbound attachment.
// Purged/never-retained content has a known commitment but cannot be rehashed.
type ArtifactVerification struct {
	State    RetentionState
	Bound    bool
	Verified bool
}

// Verify authenticates the Capsule, authorizes its signer against caller-owned
// trusted keys, and verifies every available bound original using emit's JCS.
// It does not verify CLL inclusion, external references, or business correctness.
func Verify(record Record, trusted []ed25519.PublicKey) (map[string]ArtifactVerification, error) {
	if !idPattern.MatchString(record.CapsuleID) {
		return nil, fmt.Errorf("%w: capsule id", ErrInvalid)
	}
	v, err := emit.VerifyCapsule(record.Capsule)
	if err != nil || !v.OK || v.CapsuleID == nil || *v.CapsuleID != record.CapsuleID {
		return nil, fmt.Errorf("%w: capsule verification: %v", ErrInvalid, err)
	}
	e, err := emit.VerifyEnvelope(record.CapsuleID, record.ProducerEnvelope)
	if err != nil {
		return nil, fmt.Errorf("%w: envelope: %v", ErrInvalid, err)
	}
	authorized := false
	for _, key := range trusted {
		if len(key) == ed25519.PublicKeySize && bytes.Equal(key, e.PublicKey) {
			authorized = true
		}
	}
	if !authorized {
		return nil, ErrUntrustedSigner
	}
	capsule, err := emit.DecodePayload(record.Capsule)
	if err != nil {
		return nil, err
	}
	results := make(map[string]ArtifactVerification, len(record.Artifacts))
	for _, a := range record.Artifacts {
		if !namePattern.MatchString(a.Name) {
			return nil, fmt.Errorf("%w: artifact name", ErrInvalid)
		}
		if _, exists := results[a.Name]; exists {
			return nil, fmt.Errorf("%w: duplicate artifact %s", ErrInvalid, a.Name)
		}
		var committed string
		if a.Binding != "" {
			committed, err = digestAt(capsule, a.Binding)
			if err != nil {
				return nil, err
			}
		}
		check := ArtifactVerification{State: a.State, Bound: a.Binding != ""}
		switch a.State {
		case Present:
			if a.Content == nil {
				return nil, fmt.Errorf("%w: missing content for %s", ErrInvalid, a.Name)
			}
			if a.ContentSHA256 != "" && a.ContentSHA256 != rawDigest(a.Content) {
				return nil, fmt.Errorf("%w: %s", ErrCorrupt, a.Name)
			}
			if a.Binding != "" {
				digest, digestErr := emit.DigestJSON(json.RawMessage(a.Content))
				if digestErr != nil || digest != committed {
					return nil, fmt.Errorf("%w: %s (%s)", ErrDigestMismatch, a.Name, a.Binding)
				}
				check.Verified = true
			}
		case Purged:
			if a.Content != nil || !idPattern.MatchString(a.ContentSHA256) {
				return nil, fmt.Errorf("%w: invalid purge tombstone", ErrInvalid)
			}
		case NeverRetained:
			if a.Content != nil || a.ContentSHA256 != "" {
				return nil, fmt.Errorf("%w: never-retained content", ErrInvalid)
			}
		default:
			return nil, fmt.Errorf("%w: retention state", ErrInvalid)
		}
		results[a.Name] = check
	}
	return results, nil
}

func digestAt(capsule map[string]any, field DigestField) (string, error) {
	switch field {
	case PayloadDigest, AgentOutputDigest, EffectRequestDigest, EffectResponseDigest:
	default:
		return "", fmt.Errorf("%w: unsupported digest field %s", ErrInvalid, field)
	}
	var value any = capsule
	for _, key := range strings.Split(string(field), ".") {
		obj, ok := value.(map[string]any)
		if !ok {
			return "", fmt.Errorf("%w: missing %s", ErrInvalid, field)
		}
		value, ok = obj[key]
		if !ok {
			return "", fmt.Errorf("%w: missing %s", ErrInvalid, field)
		}
	}
	digest, ok := value.(string)
	if !ok || !idPattern.MatchString(digest) {
		return "", fmt.Errorf("%w: malformed %s", ErrInvalid, field)
	}
	return digest, nil
}

func rawDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// StorageChecksum commits the exact envelope/Capsule and complete artifact
// inventory, including original byte hashes. It survives intentional purge
// while detecting missing rows, changed bindings, or accidental corruption.
// This storage checksum is NOT an additional producer authentication claim.
//
// It marshals with encoding/json/v2, matching the rest of this library's JSON
// handling. v2 encodes an empty inventory as [] rather than v1's null, so the
// checksum agrees with a JavaScript reader's JSON.stringify over the same shape;
// a shared database therefore stays portable across the Go and TypeScript stores.
func (record Record) StorageChecksum() (string, error) {
	record.Artifacts = append([]Artifact(nil), record.Artifacts...)
	for i := range record.Artifacts {
		record.Artifacts[i].Content = nil
		if record.Artifacts[i].State == Purged {
			record.Artifacts[i].State = Present
		}
	}
	sort.Slice(record.Artifacts, func(i, j int) bool { return record.Artifacts[i].Name < record.Artifacts[j].Name })
	data, err := jsonv2.Marshal(record)
	if err != nil {
		return "", err
	}
	return rawDigest(data), nil
}

// Store is the backend-neutral artifact lifecycle. Transactions are exposed by
// backends separately so this interface does not depend on database/sql.
type Store interface {
	Put(context.Context, Record) error
	Get(context.Context, string) (Record, error)
	Purge(context.Context, string) error
}

// Prepare copies a caller record's mutable slices, fills exact-byte checksums,
// and enforces v1 admission limits. It does not authenticate a producer; backends
// must also call Verify with caller-configured trusted keys before persistence.
func Prepare(record Record) (Record, error) {
	record.Capsule = append([]byte(nil), record.Capsule...)
	record.ProducerEnvelope = append([]byte(nil), record.ProducerEnvelope...)
	record.Artifacts = append([]Artifact(nil), record.Artifacts...)
	size := len(record.Capsule) + len(record.ProducerEnvelope)
	if len(record.Artifacts) > 64 || len(record.ProducerEnvelope) > 65535 {
		return Record{}, fmt.Errorf("%w: record limits", ErrInvalid)
	}
	for i := range record.Artifacts {
		a := &record.Artifacts[i]
		if a.State == Purged {
			return Record{}, ErrPurged
		}
		size += len(a.Content)
		if a.Content != nil {
			a.Content = append([]byte{}, a.Content...)
		}
		if a.State == Present {
			digest := rawDigest(a.Content)
			if a.ContentSHA256 != "" && a.ContentSHA256 != digest {
				return Record{}, ErrCorrupt
			}
			a.ContentSHA256 = digest
		}
	}
	if size > 8*1024*1024 {
		return Record{}, fmt.Errorf("%w: record exceeds 8 MiB", ErrInvalid)
	}
	return record, nil
}
