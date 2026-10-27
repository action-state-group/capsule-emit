package artifact

import "context"

// Limits of the optional index extension (docs/artifact-store.md).
const (
	// MaxIndexPaths is the most paths one namespace may declare.
	MaxIndexPaths = 32
	// DefaultFindLimit is the page size Find uses when Query.Limit is zero.
	DefaultFindLimit = 100
	// MaxFindLimit is the largest page Find returns.
	MaxFindLimit = 1000
)

// Query selects records by the namespace's declared index paths.
type Query struct {
	// Where maps declared paths to the value each must hold. A record matches
	// a path whose value is that string, or an array containing it. Every
	// entry must match.
	Where map[string]string
	// Since, when not empty, is an RFC 3339 date-time; only records whose
	// top-level timestamp is at or after it match.
	Since string
	// After is the Next of a previous page, or empty for the first page.
	After string
	// Limit is the page size, 1 to MaxFindLimit; zero means DefaultFindLimit.
	Limit int
}

// Page is one page of Find results.
type Page struct {
	// IDs are the matching Capsule IDs in the store's admission order. That
	// is not the order of any CLL log.
	IDs []string
	// Next is the cursor for the following page, empty when there is none.
	Next string
}

// IndexStore is the optional index extension: a namespace declares JSON paths
// into its Capsules, the store extracts their values at Put, and Find returns
// matching Capsule IDs without reading record bodies. Every built-in backend
// implements it; detect it with a type assertion:
//
//	if is, ok := store.(artifact.IndexStore); ok { ... }
type IndexStore interface {
	Store
	// InitIndex provisions storage as Init does, then declares paths and
	// indexes the records already stored. Declaring the same set again does
	// nothing; a different set is ErrConflict.
	InitIndex(ctx context.Context, paths []string) error
	// Reindex replaces the declared paths and rebuilds the index.
	Reindex(ctx context.Context, paths []string) error
	// Find returns one page of matching Capsule IDs.
	Find(ctx context.Context, q Query) (Page, error)
}
