// Package jsonl implements the artifact Store on a flat JSONL file.
// Each line is the exact JSON serialization of an artifact.Record using
// encoding/json/v2; the namespace is a construction-time scope and is NOT
// persisted per line. A single file holds exactly one namespace.
//
// The format is intentionally portable: StorageChecksum agrees with a
// TypeScript reader's JSON.stringify over the same record shape because
// encoding/json/v2 encodes an empty artifacts slice as [] (not null).
//
// Suitable for a single writer. No multi-process file locking is provided.
package jsonl

import (
	"bufio"
	"context"
	"crypto/ed25519"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sync"

	"github.com/action-state-group/capsule-emit-go/artifact"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var idPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

type indexEntry struct {
	offset int64
	length int
}

// Store is a file-backed artifact store. Each persisted record occupies exactly
// one UTF-8 line terminated by \n. The in-memory index maps capsule_id to
// (offset, length) in the file, built by a single scan on New.
type Store struct {
	path      string
	namespace string
	trusted   []ed25519.PublicKey
	mu        sync.Mutex
	index     map[string]indexEntry
}

// New validates the namespace and trusted keys, then opens the file and builds
// the in-memory index if it already exists. The file is not created here; call
// Init to create it if absent.
func New(path string, namespace string, trusted []ed25519.PublicKey) (*Store, error) {
	if !namePattern.MatchString(namespace) {
		return nil, fmt.Errorf("%w: namespace", artifact.ErrInvalid)
	}
	if len(trusted) == 0 {
		return nil, fmt.Errorf("%w: no trusted keys", artifact.ErrInvalid)
	}
	s := &Store{
		path:      path,
		namespace: namespace,
		index:     make(map[string]indexEntry),
	}
	for _, key := range trusted {
		if len(key) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("%w: public key size", artifact.ErrInvalid)
		}
		s.trusted = append(s.trusted, append(ed25519.PublicKey(nil), key...))
	}
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return s, nil
		}
		return nil, err
	}
	defer f.Close()
	if err := s.scanIndex(f); err != nil {
		return nil, err
	}
	return s, nil
}

// scanIndex reads every line from r and populates s.index.
// Lines that cannot yield a capsule_id are silently skipped in the index; Get
// will return ErrCorrupt for lines that parse enough to be indexed but whose
// full record unmarshal fails. Last-occurrence-wins for duplicate capsule_ids.
func (s *Store) scanIndex(r io.Reader) error {
	const maxLine = 16 * 1024 * 1024
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, maxLine), maxLine)
	var offset int64
	for sc.Scan() {
		line := sc.Bytes()
		length := len(line)
		var peek struct {
			CapsuleID string `json:"capsule_id"`
		}
		if err := jsonv2.Unmarshal(line, &peek); err == nil && peek.CapsuleID != "" {
			// Last-occurrence-wins as a defensive tie-break only.
			s.index[peek.CapsuleID] = indexEntry{offset: offset, length: length}
		}
		offset += int64(length) + 1 // +1 for the \n terminator
	}
	return sc.Err()
}

// Namespace returns the explicit storage scope.
func (s *Store) Namespace() string { return s.namespace }

// Init creates an empty file at s.path if it does not yet exist.
// Calling Init on an existing file is a no-op.
func (s *Store) Init(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := os.OpenFile(s.path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return nil
		}
		return err
	}
	return f.Close()
}

// Put persists an immutable record. Prepare and Verify are called in that order,
// matching sqlite's PutTx semantics. Byte-identical retries are admitted. A
// conflicting record (same capsule_id, different StorageChecksum) returns
// ErrConflict. Calling Put after Purge for the same capsule_id returns ErrPurged.
func (s *Store) Put(ctx context.Context, record artifact.Record) error {
	prepared, err := artifact.Prepare(record)
	if err != nil {
		return err
	}
	record = prepared
	if _, err := artifact.Verify(record, s.trusted); err != nil {
		return err
	}
	hash, err := record.StorageChecksum()
	if err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if entry, exists := s.index[record.CapsuleID]; exists {
		existing, err := s.readAt(entry)
		if err != nil {
			return err
		}
		if existing.CapsuleID != record.CapsuleID {
			return fmt.Errorf("%w: index points to a different capsule", artifact.ErrCorrupt)
		}
		// Reads are fail-closed: verify the stored record before admitting a
		// retry, matching Get and the TypeScript backend.
		if _, err := artifact.Verify(existing, s.trusted); err != nil {
			return err
		}
		oldHash, err := existing.StorageChecksum()
		if err != nil {
			return err
		}
		if oldHash != hash {
			return artifact.ErrConflict
		}
		// Retry admitted only when no artifact has been purged.
		for _, a := range existing.Artifacts {
			if a.State == artifact.Purged {
				return artifact.ErrPurged
			}
		}
		return nil
	}

	data, err := jsonv2.Marshal(record)
	if err != nil {
		return err
	}

	f, err := os.OpenFile(s.path, os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()

	offset, err := f.Seek(0, io.SeekEnd)
	if err != nil {
		return err
	}

	// Write data + newline as a single call to avoid a partial line on error.
	line := make([]byte, len(data)+1)
	copy(line, data)
	line[len(data)] = '\n'
	if _, err := f.Write(line); err != nil {
		return err
	}

	s.index[record.CapsuleID] = indexEntry{offset: offset, length: len(data)}
	return nil
}

// Get loads a record by capsule ID, verifying the signer against trusted keys.
// Returns ErrInvalid for a malformed id, ErrNotFound if absent, ErrCorrupt if
// the stored line cannot be parsed, and ErrUntrustedSigner if the signer is
// not in the trusted set (fail-closed).
func (s *Store) Get(ctx context.Context, id string) (artifact.Record, error) {
	if !idPattern.MatchString(id) {
		return artifact.Record{}, artifact.ErrInvalid
	}
	s.mu.Lock()
	entry, exists := s.index[id]
	if !exists {
		s.mu.Unlock()
		return artifact.Record{}, artifact.ErrNotFound
	}
	// Hold the lock across the read so a concurrent Purge cannot rename the file
	// and invalidate entry's offset between the index lookup and the read.
	record, err := s.readAt(entry)
	s.mu.Unlock()
	if err != nil {
		return artifact.Record{}, err
	}
	if record.CapsuleID != id {
		return artifact.Record{}, fmt.Errorf("%w: index points to a different capsule", artifact.ErrCorrupt)
	}
	if _, err := artifact.Verify(record, s.trusted); err != nil {
		return artifact.Record{}, err
	}
	return record, nil
}

// readAt reads and unmarshals the record line described by entry.
// Returns ErrCorrupt if the bytes cannot be read or parsed as an artifact.Record.
func (s *Store) readAt(entry indexEntry) (artifact.Record, error) {
	f, err := os.Open(s.path)
	if err != nil {
		return artifact.Record{}, err
	}
	defer f.Close()
	buf := make([]byte, entry.length)
	if _, err := f.ReadAt(buf, entry.offset); err != nil {
		return artifact.Record{}, fmt.Errorf("%w: read failed: %v", artifact.ErrCorrupt, err)
	}
	var record artifact.Record
	if err := jsonv2.Unmarshal(buf, &record); err != nil {
		return artifact.Record{}, fmt.Errorf("%w: parse failed: %v", artifact.ErrCorrupt, err)
	}
	return record, nil
}

// Purge erases business originals for all Present artifacts in the named record,
// replacing each with a tombstone (Content dropped, State set to Purged,
// ContentSHA256 preserved). Already-purged and never-retained artifacts are
// unchanged. Purge is idempotent. The StorageChecksum is invariant across purge
// because StorageChecksum normalizes Purged back to Present and drops Content.
//
// The whole file is rewritten to a temp file, then renamed over the original.
func (s *Store) Purge(ctx context.Context, id string) error {
	if !idPattern.MatchString(id) {
		return artifact.ErrInvalid
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, exists := s.index[id]
	if !exists {
		return artifact.ErrNotFound
	}
	target, err := s.readAt(entry)
	if err != nil {
		return err
	}
	if target.CapsuleID != id {
		return fmt.Errorf("%w: index points to a different capsule", artifact.ErrCorrupt)
	}
	purged := target
	purged.Artifacts = append([]artifact.Artifact(nil), target.Artifacts...)
	for i := range purged.Artifacts {
		if purged.Artifacts[i].State == artifact.Present {
			purged.Artifacts[i].Content = nil
			purged.Artifacts[i].State = artifact.Purged
		}
	}
	return s.rewrite(id, purged)
}

// rewrite copies every line of the file to a temp file, replacing the line for
// targetID with the marshaled replacement, then renames the temp file over the
// original and rebuilds the in-memory index.
func (s *Store) rewrite(targetID string, replacement artifact.Record) (err error) {
	src, err := os.Open(s.path)
	if err != nil {
		return err
	}
	defer src.Close()

	dir := filepath.Dir(s.path)
	tmp, err := os.CreateTemp(dir, ".jsonl-rewrite-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()

	// A single cleanup closes the temp file and removes it unless the rewrite
	// commits via a successful rename, in place of a manual close on every
	// error return. A second close after the commit path is a harmless no-op.
	committed := false
	defer func() {
		if !committed {
			_ = tmp.Close()
			_ = os.Remove(tmpPath)
		}
	}()

	// Preserve the source file's permission bits; os.CreateTemp makes 0600,
	// which would otherwise narrow an initialized 0644 store on first purge.
	// A stat/chmod failure aborts rather than silently narrowing the store.
	info, err := src.Stat()
	if err != nil {
		return err
	}
	if err = tmp.Chmod(info.Mode().Perm()); err != nil {
		return err
	}

	const maxLine = 16 * 1024 * 1024
	sc := bufio.NewScanner(src)
	sc.Buffer(make([]byte, maxLine), maxLine)
	w := bufio.NewWriter(tmp)
	newIndex := make(map[string]indexEntry, len(s.index))
	var offset int64
	found := false

	for sc.Scan() {
		line := sc.Bytes()
		var peek struct {
			CapsuleID string `json:"capsule_id"`
		}
		_ = jsonv2.Unmarshal(line, &peek)

		var outLine []byte
		if peek.CapsuleID == targetID {
			found = true
			if outLine, err = jsonv2.Marshal(replacement); err != nil {
				return err
			}
		} else {
			// Copy the original line bytes verbatim.
			outLine = append([]byte(nil), line...)
		}

		if _, err = w.Write(outLine); err != nil {
			return err
		}
		if err = w.WriteByte('\n'); err != nil {
			return err
		}
		if peek.CapsuleID != "" {
			newIndex[peek.CapsuleID] = indexEntry{offset: offset, length: len(outLine)}
		}
		offset += int64(len(outLine)) + 1
	}
	if err = sc.Err(); err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("%w: target line vanished during rewrite", artifact.ErrCorrupt)
	}
	if err = w.Flush(); err != nil {
		return err
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	if err = os.Rename(tmpPath, s.path); err != nil {
		return err
	}
	s.index = newIndex
	committed = true
	return nil
}
