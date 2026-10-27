// Command interlock is the Go adapter of the cross-language artifact store
// interlock (.github/scripts/artifact-store-interlock.sh). It runs one phase of
// test-vectors/artifact-store/interlock.json against one store and prints one
// report line per step; every language's adapter must print the same bytes.
//
//	go run ./artifact/internal/interlock <phase> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>
//
// target is a file path for jsonl and sqlite, and host:port/database for mysql.
package main

import (
	"context"
	"crypto/ed25519"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/action-state-group/capsule-emit/go/artifact"
	jsonlstore "github.com/action-state-group/capsule-emit/go/artifact/jsonl"
	mysqlstore "github.com/action-state-group/capsule-emit/go/artifact/mysql"
	sqlitestore "github.com/action-state-group/capsule-emit/go/artifact/sqlite"
	_ "github.com/go-sql-driver/mysql"
	_ "modernc.org/sqlite"
)

type step struct {
	Op     string            `json:"op"`
	Record string            `json:"record"`
	ID     *string           `json:"id"`
	Paths  []string          `json:"paths"`
	Where  map[string]string `json:"where"`
}

// plan is interlock.json: steps per phase, and the trusted key names per
// phase (a phase without its own entry uses "default").
type plan struct {
	Trusted map[string][]string `json:"trusted"`
	Phases  map[string][]step   `json:"phases"`
}

type vectors struct {
	Keys    map[string]string          `json:"keys"`
	Records map[string]json.RawMessage `json:"records"`
}

var codes = []struct {
	err  error
	code string
}{
	{artifact.ErrInvalid, "invalid"}, {artifact.ErrConflict, "conflict"},
	{artifact.ErrNotFound, "not_found"}, {artifact.ErrPurged, "purged"},
	{artifact.ErrDigestMismatch, "digest_mismatch"}, {artifact.ErrUntrustedSigner, "untrusted_signer"},
	{artifact.ErrCorrupt, "corrupt"},
}

func outcome(err error) string {
	if err == nil {
		return "ok"
	}
	for _, c := range codes {
		if errors.Is(err, c.err) {
			return c.code
		}
	}
	fmt.Fprintln(os.Stderr, "storage error:", err)
	return "storage_error"
}

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func readJSON(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

func run(args []string, out io.Writer) error {
	if len(args) != 5 {
		return fmt.Errorf("usage: interlock <phase> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>")
	}
	phase, format, target, namespace, dir := args[0], args[1], args[2], args[3], args[4]
	var p plan
	var v vectors
	if err := readJSON(filepath.Join(dir, "interlock.json"), &p); err != nil {
		return err
	}
	if err := readJSON(filepath.Join(dir, "vectors.json"), &v); err != nil {
		return err
	}
	steps, ok := p.Phases[phase]
	if !ok {
		return fmt.Errorf("unknown phase %q", phase)
	}
	trusted, ok := p.Trusted[phase]
	if !ok {
		trusted = p.Trusted["default"]
	}
	var keys []ed25519.PublicKey
	for _, name := range trusted {
		key, err := hex.DecodeString(v.Keys[name])
		if err != nil {
			return err
		}
		keys = append(keys, key)
	}
	ctx := context.Background()
	var store artifact.Store
	var err error
	switch format {
	case "jsonl":
		store, err = jsonlstore.New(target, namespace, keys)
	case "sqlite":
		uri := (&url.URL{Scheme: "file", Path: target, RawQuery: "mode=rwc"}).String()
		db, openErr := sql.Open("sqlite", uri)
		if openErr != nil {
			return openErr
		}
		defer db.Close()
		db.SetMaxOpenConns(1)
		if _, err := db.Exec("PRAGMA foreign_keys=ON"); err != nil {
			return err
		}
		store, err = sqlitestore.New(db, namespace, keys)
	case "mysql":
		addr, database, _ := strings.Cut(target, "/")
		db, openErr := sql.Open("mysql", fmt.Sprintf("root@tcp(%s)/%s", addr, database))
		if openErr != nil {
			return openErr
		}
		defer db.Close()
		store, err = mysqlstore.New(db, namespace, keys)
	default:
		return fmt.Errorf("unknown format %q", format)
	}
	if err != nil {
		return err
	}
	if phase == "write" {
		if err := store.Init(ctx); err != nil {
			return err
		}
	}
	for i, s := range steps {
		id := ""
		if s.ID != nil {
			id = *s.ID
		} else if raw, ok := v.Records[s.Record]; ok {
			var r struct {
				CapsuleID string `json:"capsule_id"`
			}
			if err := json.Unmarshal(raw, &r); err != nil {
				return err
			}
			id = r.CapsuleID
		}
		subject := s.Record
		if s.ID != nil {
			quoted, _ := jsonv2.Marshal(*s.ID)
			subject = string(quoted)
		}
		// Index steps name their paths or query in compact JSON.
		switch s.Op {
		case "init_index":
			quoted, _ := jsonv2.Marshal(s.Paths)
			subject = string(quoted)
		case "find":
			quoted, _ := jsonv2.Marshal(s.Where, jsonv2.Deterministic(true))
			subject = string(quoted)
		}
		line := fmt.Sprintf("%d\t%s %s\t", i, s.Op, subject)
		switch s.Op {
		case "put":
			raw, known := v.Records[s.Record]
			if !known {
				return fmt.Errorf("unknown record %q", s.Record)
			}
			// A record the codec cannot represent is invalid input.
			var record artifact.Record
			if err := jsonv2.Unmarshal(raw, &record); err != nil {
				line += "invalid"
			} else {
				line += outcome(store.Put(ctx, record))
			}
		case "purge":
			line += outcome(store.Purge(ctx, id))
		case "init_index":
			line += outcome(store.(artifact.IndexStore).InitIndex(ctx, s.Paths))
		case "find":
			// The interlock's finds fit one page; IDs print comma-separated.
			page, err := store.(artifact.IndexStore).Find(ctx, artifact.Query{Where: s.Where, Limit: artifact.MaxFindLimit})
			line += outcome(err)
			if err == nil && len(page.IDs) > 0 {
				line += "\t" + strings.Join(page.IDs, ",")
			}
		case "get":
			got, err := store.Get(ctx, id)
			line += outcome(err)
			if err == nil {
				sum, err := got.StorageChecksum()
				if err != nil {
					return err
				}
				sort.Slice(got.Artifacts, func(a, b int) bool { return got.Artifacts[a].Name < got.Artifacts[b].Name })
				encoded, err := jsonv2.Marshal(got)
				if err != nil {
					return err
				}
				line += "\t" + sum + "\t" + string(encoded)
			}
		default:
			return fmt.Errorf("unknown op %q", s.Op)
		}
		fmt.Fprintln(out, strings.TrimRight(line, "\t"))
	}
	return nil
}
