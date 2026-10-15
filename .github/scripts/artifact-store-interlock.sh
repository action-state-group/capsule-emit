#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Cross-language artifact store interlock (docs/artifact-store.md), for one
# storage format: jsonl, sqlite or mysql.
#
# Every language's adapter runs test-vectors/artifact-store/interlock.json:
#
# 1. write: each language writes the sequence (admissions, retries, conflicts,
#    rejections with their error codes, purges) to its own store. The reports
#    must be byte-identical to each other and to interlock-write.txt.
# 2. storage: the JSONL files, or the SQL rows (artifact_store_rows.py), must
#    be byte-identical across languages.
# 3. read: every language reads its own copy of every language's store (all
#    pairs): each record's outcome, StorageChecksum and wire line, then
#    retries, a conflict and purges of retained content against that data.
#    All reports must equal interlock-read.txt, every copy must end in the
#    same state, and existing SQL rows must keep their exact created_at.
#
# A language joins by adding its adapter (il_<lang>, and setup_<lang> if it
# needs a build) below, and its name and toolchain to
# .github/workflows/artifact-interlock.yml, in the pull request that adds its
# backend.
# MySQL needs CAPSULE_STORAGE_TEST_MYSQL=host:port with a disposable
# capsule_storage_test database.
set -euo pipefail

format=${1:?usage: artifact-store-interlock.sh <jsonl|sqlite|mysql>}
repo=$(cd "$(dirname "$0")/../.." && pwd)
vectors="$repo/test-vectors/artifact-store"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
python_bin=${PYTHON:-python3}
read -r -a languages <<< "${LANGUAGES:-go ts}"

# Each language: an optional setup_<lang> (build once) and its adapter il_<lang>.
setup_go() { (cd "$repo/go" && go build -o "$work/go-interlock" ./artifact/internal/interlock); }
il_go() { "$work/go-interlock" "$@"; }
il_ts() { node "$repo/ts/scripts/artifact-interlock.mjs" "$@"; }

for lang in "${languages[@]}"; do
  if declare -F "setup_$lang" > /dev/null; then "setup_$lang"; fi
done

rows() { "$python_bin" "$repo/.github/scripts/artifact_store_rows.py" "$@"; }

# store <name> prints "<target> <namespace>" for a store: a file per store for
# jsonl and sqlite, a namespace per store in the one MySQL database.
store() {
  case "$format" in
    jsonl) echo "$work/$1.jsonl interlock" ;;
    sqlite) echo "$work/$1.db interlock" ;;
    mysql) echo "${CAPSULE_STORAGE_TEST_MYSQL:?set CAPSULE_STORAGE_TEST_MYSQL} interlock-$1" ;;
    *) echo "unknown format $format" >&2; exit 2 ;;
  esac
}

# dump <target> <namespace>: the store's bytes (jsonl) or normalized rows.
dump() {
  if test "$format" = jsonl; then cat "$1"; else rows dump "$format" "$1" "$2"; fi
}

same() {
  if ! cmp -s "$1" "$2"; then
    echo "FAIL: $3" >&2
    diff "$1" "$2" | head -20 >&2 || true
    exit 1
  fi
}

# 1. write: every language writes the sequence to its own store.
for lang in "${languages[@]}"; do
  read -r target ns <<< "$(store "$lang")"
  if test "$format" = mysql; then rows reset mysql "$target" "$ns"; fi
  "il_$lang" write "$format" "$target" "$ns" "$vectors" > "$work/write-$lang.txt"
  same "$vectors/interlock-write.txt" "$work/write-$lang.txt" "$lang write report ($format)"
done
echo "write: ${languages[*]} produce the expected report ($format)"

# 2. storage: every language wrote the same bytes or rows.
first=${languages[0]}
for lang in "${languages[@]}"; do
  dump $(store "$lang") > "$work/storage-$lang.txt"
  same "$work/storage-$first.txt" "$work/storage-$lang.txt" "$lang storage differs from $first ($format)"
done
echo "storage: ${languages[*]} wrote identical $format data"

# 3. read: every reader against a fresh copy of every writer's store (all
# pairs). The read phase also retries, conflicts and purges retained content,
# so each pair gets its own copy; every copy must end in the same state, and
# the SQL rows that existed before must keep their exact created_at.
for reader in "${languages[@]}"; do
  for writer in "${languages[@]}"; do
    pair="$reader-$writer"
    read -r source source_ns <<< "$(store "$writer")"
    read -r target ns <<< "$(store "pair-$pair")"
    case "$format" in
      jsonl | sqlite) cp "$source" "$target" ;;
      mysql) rows reset mysql "$target" "$ns" && rows copy mysql "$target" "$source_ns" "$ns" ;;
    esac
    if test "$format" != jsonl; then rows created "$format" "$target" "$ns" > "$work/created-before-$pair.txt"; fi
    "il_$reader" read "$format" "$target" "$ns" "$vectors" > "$work/read-$pair.txt"
    same "$vectors/interlock-read.txt" "$work/read-$pair.txt" "$reader reading $writer's $format store"
    dump "$target" "$ns" > "$work/after-$pair.txt"
    same "$work/after-$first-$first.txt" "$work/after-$pair.txt" "$reader left $writer's $format store differently from $first"
    if test "$format" != jsonl; then
      rows created "$format" "$target" "$ns" > "$work/created-after-$pair.txt"
      same "$work/created-before-$pair.txt" "$work/created-after-$pair.txt" "$reader rewrote existing rows in $writer's $format store"
    fi
  done
done
echo "read: every pair of ${languages[*]} reads, retries and purges the other's $format store alike"
