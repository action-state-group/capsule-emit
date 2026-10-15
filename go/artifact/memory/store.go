// Package memory implements the artifact Store in process memory, for tests and
// short-lived tools. It follows the same contract as the persistent backends
// (docs/artifact-store.md) but keeps nothing across process restarts and has no
// transactional extension. It links no database driver.
package memory

import (
	"context"
	"crypto/ed25519"
	"fmt"
	"regexp"
	"sync"

	"github.com/action-state-group/capsule-emit/go/artifact"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var idPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

var _ artifact.Store = (*Store)(nil)

// Store holds prepared records by Capsule ID. It is safe for concurrent use.
type Store struct {
	namespace string
	trusted   []ed25519.PublicKey
	mu        sync.Mutex
	records   map[string]artifact.Record
}

// New validates the namespace and trusted keys. The store starts empty.
func New(namespace string, trusted []ed25519.PublicKey) (*Store, error) {
	if !namePattern.MatchString(namespace) || len(trusted) == 0 {
		return nil, artifact.ErrInvalid
	}
	s := &Store{namespace: namespace, records: map[string]artifact.Record{}}
	for _, key := range trusted {
		if len(key) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("%w: public key size", artifact.ErrInvalid)
		}
		s.trusted = append(s.trusted, append(ed25519.PublicKey(nil), key...))
	}
	return s, nil
}

// Namespace returns the explicit storage scope.
func (s *Store) Namespace() string { return s.namespace }

// Init has nothing to provision.
func (s *Store) Init(context.Context) error { return nil }

// Put admits an immutable record, accepting byte-identical retries.
func (s *Store) Put(_ context.Context, record artifact.Record) error {
	prepared, err := artifact.Prepare(record)
	if err != nil {
		return err
	}
	if _, err := artifact.Verify(prepared, s.trusted); err != nil {
		return err
	}
	hash, err := prepared.StorageChecksum()
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	existing, exists := s.records[prepared.CapsuleID]
	if !exists {
		s.records[prepared.CapsuleID] = prepared
		return nil
	}
	// existing was verified with these same keys when it was admitted, and
	// memory cannot be altered behind the store, so it needs no re-verification.
	oldHash, err := existing.StorageChecksum()
	if err != nil {
		return err
	}
	if oldHash != hash {
		return artifact.ErrConflict
	}
	for _, a := range existing.Artifacts {
		if a.State == artifact.Purged {
			return artifact.ErrPurged
		}
	}
	return nil
}

// Get returns a copy of the stored record after verifying it.
func (s *Store) Get(_ context.Context, id string) (artifact.Record, error) {
	if !idPattern.MatchString(id) {
		return artifact.Record{}, artifact.ErrInvalid
	}
	s.mu.Lock()
	stored, exists := s.records[id]
	s.mu.Unlock()
	if !exists {
		return artifact.Record{}, artifact.ErrNotFound
	}
	record := clone(stored)
	if _, err := artifact.Verify(record, s.trusted); err != nil {
		return artifact.Record{}, err
	}
	return record, nil
}

// Purge replaces each present original with a tombstone that keeps its SHA-256.
// It requires only the record's existence, not its verification.
func (s *Store) Purge(_ context.Context, id string) error {
	if !idPattern.MatchString(id) {
		return artifact.ErrInvalid
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	stored, exists := s.records[id]
	if !exists {
		return artifact.ErrNotFound
	}
	purged := clone(stored)
	for i := range purged.Artifacts {
		if purged.Artifacts[i].State == artifact.Present {
			purged.Artifacts[i].Content = nil
			purged.Artifacts[i].State = artifact.Purged
		}
	}
	s.records[id] = purged
	return nil
}

// clone deep-copies a record so callers never alias stored bytes. Records are
// stored after Prepare, which already copied the caller's buffers.
func clone(r artifact.Record) artifact.Record {
	r.Capsule = append([]byte(nil), r.Capsule...)
	r.ProducerEnvelope = append([]byte(nil), r.ProducerEnvelope...)
	r.Artifacts = append([]artifact.Artifact{}, r.Artifacts...)
	for i := range r.Artifacts {
		if r.Artifacts[i].Content != nil {
			r.Artifacts[i].Content = append([]byte{}, r.Artifacts[i].Content...)
		}
	}
	return r
}
