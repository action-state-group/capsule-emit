// Package index holds the parts of the artifact store's index extension
// (docs/artifact-store.md, "Optional index extension") that every backend
// shares: path validation, value extraction from Capsule bytes, timestamp
// normalization, query checks, cursors, and the in-memory index that the
// jsonl and memory backends keep.
package index

import (
	"crypto/sha256"
	"encoding/hex"
	jsonv2 "encoding/json/v2"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/action-state-group/capsule-emit/go/artifact"
)

var segmentPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

var timestampPattern = regexp.MustCompile(`^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$`)

// NormalizePaths validates a declared path list and returns it sorted. The
// list is a set: order does not matter and a repeated path is invalid.
func NormalizePaths(paths []string) ([]string, error) {
	if len(paths) > artifact.MaxIndexPaths {
		return nil, fmt.Errorf("%w: more than %d index paths", artifact.ErrInvalid, artifact.MaxIndexPaths)
	}
	sorted := slices.Clone(paths)
	slices.Sort(sorted)
	for i, path := range sorted {
		if !validPath(path) {
			return nil, fmt.Errorf("%w: index path %q", artifact.ErrInvalid, path)
		}
		if i > 0 && sorted[i-1] == path {
			return nil, fmt.Errorf("%w: repeated index path %q", artifact.ErrInvalid, path)
		}
	}
	if sorted == nil {
		sorted = []string{}
	}
	return sorted, nil
}

func validPath(path string) bool {
	if len(path) > 255 {
		return false
	}
	segments := strings.Split(path, ".")
	if len(segments) > 8 {
		return false
	}
	for _, segment := range segments {
		if !segmentPattern.MatchString(segment) {
			return false
		}
	}
	return true
}

// EncodePaths is the stored form of a normalized path list: a compact JSON
// array. Path characters need no escaping, so every language writes the same
// text.
func EncodePaths(paths []string) string {
	return `[` + strings.Join(wrap(paths), ",") + `]`
}

func wrap(paths []string) []string {
	out := make([]string, len(paths))
	for i, p := range paths {
		out[i] = `"` + p + `"`
	}
	return out
}

// DecodePaths reads EncodePaths output back.
func DecodePaths(stored string) ([]string, error) {
	var paths []string
	if err := jsonv2.Unmarshal([]byte(stored), &paths); err != nil || paths == nil {
		return nil, fmt.Errorf("%w: stored index paths: %v", artifact.ErrCorrupt, err)
	}
	normalized, err := NormalizePaths(paths)
	if err != nil {
		return nil, fmt.Errorf("%w: stored index paths: %v", artifact.ErrCorrupt, err)
	}
	return normalized, nil
}

// Entry is one indexed value of one path.
type Entry struct {
	Path  string
	Value string
}

// Extracted is what a record contributes to the index.
type Extracted struct {
	Entries []Entry
	// TS is the Capsule's top-level timestamp in microseconds since the Unix
	// epoch, valid only when HasTS.
	TS    int64
	HasTS bool
}

// Extract reads the declared paths and the top-level timestamp from Capsule
// bytes. A string value is one entry and each distinct string in an array is
// one entry; any other value, or a missing member, gives none. The bytes were
// verified at admission, so a decode failure is reported as corrupt.
func Extract(capsule []byte, paths []string) (Extracted, error) {
	var root any
	if err := jsonv2.Unmarshal(capsule, &root); err != nil {
		return Extracted{}, fmt.Errorf("%w: capsule is not JSON: %v", artifact.ErrCorrupt, err)
	}
	var out Extracted
	object, _ := root.(map[string]any)
	if s, ok := object["timestamp"].(string); ok {
		out.TS, out.HasTS = ParseTimestamp(s)
	}
	for _, path := range paths {
		var value any = object
		for _, segment := range strings.Split(path, ".") {
			m, ok := value.(map[string]any)
			if !ok {
				value = nil
				break
			}
			value = m[segment]
		}
		switch v := value.(type) {
		case string:
			out.Entries = append(out.Entries, Entry{Path: path, Value: v})
		case []any:
			seen := map[string]bool{}
			for _, element := range v {
				if s, ok := element.(string); ok && !seen[s] {
					seen[s] = true
					out.Entries = append(out.Entries, Entry{Path: path, Value: s})
				}
			}
		}
	}
	return out, nil
}

// ParseTimestamp reads an RFC 3339 date-time with an upper-case T and Z (or a
// numeric offset) and at most nine fraction digits, and returns it in UTC
// microseconds since the Unix epoch, truncating finer digits.
func ParseTimestamp(s string) (int64, bool) {
	m := timestampPattern.FindStringSubmatch(s)
	if m == nil {
		return 0, false
	}
	n := func(i int) int { v, _ := strconv.Atoi(m[i]); return v }
	year, month, day, hour, minute, second := n(1), n(2), n(3), n(4), n(5), n(6)
	if month < 1 || month > 12 || day < 1 || day > daysIn(year, month) || hour > 23 || minute > 59 || second > 59 {
		return 0, false
	}
	micros := 0
	if m[7] != "" {
		digits := (m[7][1:] + "00000")[:6]
		micros, _ = strconv.Atoi(digits)
	}
	offset := 0
	if m[8] != "Z" {
		oh, _ := strconv.Atoi(m[8][1:3])
		om, _ := strconv.Atoi(m[8][4:6])
		if oh > 23 || om > 59 {
			return 0, false
		}
		offset = oh*60 + om
		if m[8][0] == '-' {
			offset = -offset
		}
	}
	t := time.Date(year, time.Month(month), day, hour, minute, second, micros*1000, time.UTC)
	return t.UnixMicro() - int64(offset)*60_000_000, true
}

func daysIn(year, month int) int {
	return time.Date(year, time.Month(month)+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// ValueKey is the key SQL backends store for a value: SHA-256 of its UTF-8
// bytes in lowercase hex, so values of any length fit an index key.
func ValueKey(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// Checked is a validated query.
type Checked struct {
	// Where holds the equalities sorted by path.
	Where    []Entry
	Since    int64
	HasSince bool
	After    int64
	Limit    int
}

// Check validates q against the declared paths.
func Check(q artifact.Query, declared []string) (Checked, error) {
	c := Checked{Limit: q.Limit}
	if c.Limit == 0 {
		c.Limit = artifact.DefaultFindLimit
	}
	if c.Limit < 1 || c.Limit > artifact.MaxFindLimit {
		return Checked{}, fmt.Errorf("%w: limit %d", artifact.ErrInvalid, q.Limit)
	}
	for path, value := range q.Where {
		if !slices.Contains(declared, path) {
			return Checked{}, fmt.Errorf("%w: %q is not a declared index path", artifact.ErrInvalid, path)
		}
		c.Where = append(c.Where, Entry{Path: path, Value: value})
	}
	slices.SortFunc(c.Where, func(a, b Entry) int { return strings.Compare(a.Path, b.Path) })
	if q.Since != "" {
		since, ok := ParseTimestamp(q.Since)
		if !ok {
			return Checked{}, fmt.Errorf("%w: since %q is not an RFC 3339 date-time", artifact.ErrInvalid, q.Since)
		}
		c.Since, c.HasSince = since, true
	}
	if q.After != "" {
		after, err := strconv.ParseInt(q.After, 10, 64)
		if err != nil || after < 1 || strconv.FormatInt(after, 10) != q.After {
			return Checked{}, fmt.Errorf("%w: cursor", artifact.ErrInvalid)
		}
		c.After = after
	}
	return c, nil
}

// Cursor formats an admission position as the opaque cursor Find returns.
func Cursor(position int64) string { return strconv.FormatInt(position, 10) }

// Memory is the in-memory index of the jsonl and memory backends. Positions
// start at 1 and follow admission order. It is not safe for concurrent use;
// the backend holds its own lock.
type Memory struct {
	paths    []string
	declared bool
	ts       []*int64                      // by position - 1
	ids      []string                      // by position - 1
	values   map[string]map[string][]int64 // path -> value -> ascending positions
	position map[string]int64              // capsule ID -> position
}

// Declared reports whether paths were declared, and returns them.
func (m *Memory) Declared() ([]string, bool) { return m.paths, m.declared }

// Declare records paths for a fresh index, or checks them against the
// existing declaration: the same set is a no-op, another is a conflict. It
// reports whether the caller must now add the existing records.
func (m *Memory) Declare(paths []string) (bool, error) {
	normalized, err := NormalizePaths(paths)
	if err != nil {
		return false, err
	}
	if m.declared {
		if !slices.Equal(m.paths, normalized) {
			return false, fmt.Errorf("%w: namespace already indexes %s", artifact.ErrConflict, EncodePaths(m.paths))
		}
		return false, nil
	}
	m.Reset(normalized)
	return true, nil
}

// Reset replaces the declaration with normalized paths and empties the index.
func (m *Memory) Reset(normalized []string) {
	*m = Memory{paths: normalized, declared: true, values: map[string]map[string][]int64{}, position: map[string]int64{}}
}

// Add indexes one record at the next position. A record already indexed is
// left where it is. Add does nothing before a declaration.
func (m *Memory) Add(id string, capsule []byte) error {
	if !m.declared {
		return nil
	}
	if _, ok := m.position[id]; ok {
		return nil
	}
	extracted, err := Extract(capsule, m.paths)
	if err != nil {
		return err
	}
	position := int64(len(m.ids) + 1)
	m.ids = append(m.ids, id)
	m.position[id] = position
	if extracted.HasTS {
		ts := extracted.TS
		m.ts = append(m.ts, &ts)
	} else {
		m.ts = append(m.ts, nil)
	}
	for _, e := range extracted.Entries {
		if m.values[e.Path] == nil {
			m.values[e.Path] = map[string][]int64{}
		}
		m.values[e.Path][e.Value] = append(m.values[e.Path][e.Value], position)
	}
	return nil
}

// Find answers q. Before a declaration it is invalid.
func (m *Memory) Find(q artifact.Query) (artifact.Page, error) {
	if !m.declared {
		return artifact.Page{}, fmt.Errorf("%w: no index paths declared", artifact.ErrInvalid)
	}
	c, err := Check(q, m.paths)
	if err != nil {
		return artifact.Page{}, err
	}
	page := artifact.Page{IDs: []string{}}
	if c.After >= int64(len(m.ids)) {
		return page, nil // also keeps After+1 below from overflowing
	}
	var candidates []int64
	if len(c.Where) == 0 {
		for p := c.After + 1; p <= int64(len(m.ids)); p++ {
			candidates = append(candidates, p)
		}
	} else {
		all := m.values[c.Where[0].Path][c.Where[0].Value]
		start, _ := slices.BinarySearch(all, c.After+1)
		candidates = all[start:]
	}
	for _, p := range candidates {
		if !m.matches(p, c) {
			continue
		}
		if len(page.IDs) == c.Limit {
			page.Next = Cursor(lastPosition(m, page))
			break
		}
		page.IDs = append(page.IDs, m.ids[p-1])
	}
	return page, nil
}

func lastPosition(m *Memory, page artifact.Page) int64 {
	return m.position[page.IDs[len(page.IDs)-1]]
}

func (m *Memory) matches(p int64, c Checked) bool {
	if c.HasSince {
		ts := m.ts[p-1]
		if ts == nil || *ts < c.Since {
			return false
		}
	}
	for _, w := range c.Where[min(1, len(c.Where)):] {
		if _, found := slices.BinarySearch(m.values[w.Path][w.Value], p); !found {
			return false
		}
	}
	return true
}
