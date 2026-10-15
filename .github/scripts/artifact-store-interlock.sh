#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Cross-language artifact store interlock (docs/artifact-store.md), for one
# storage format: jsonl, sqlite or mysql. LANGUAGES (set by
# .github/workflows/artifact-interlock.yml) lists the languages that take part.
#
# Every language's adapter runs the phases of
# test-vectors/artifact-store/interlock.json:
#
# 1. write: each language writes the sequence (admissions, retries, conflicts,
#    rejections with their error codes, purges) to its own store. Every report
#    must equal interlock-write.txt.
# 2. storage: the JSONL files, or the SQL schema and rows
#    (artifact_store_rows.py), must be byte-identical across languages, and
#    hold the expected number of records.
# 3. read and strict: every language works on its own copy of every
#    language's store (all pairs). read reports each record's outcome,
#    StorageChecksum and JSONL line, then retries, a conflict and purges of
#    retained content; strict reads with fewer trusted keys. Every report must
#    equal interlock-read.txt and interlock-strict.txt, every copy must end in
#    the same state, and existing SQL rows must keep their exact created_at.
#
# A language joins by adding its adapter (il_<lang>, and setup_<lang> if it
# needs a build) below, and its name and toolchain to the workflow, in the pull
# request that adds its backend.
#
# MySQL needs CAPSULE_STORAGE_TEST_MYSQL=host:port of a disposable server where
# root may create databases; each language gets its own database.
#
# INTERLOCK_INJECT=report|storage|read runs a negative control: it corrupts the
# last language's write report, stored data, or read report, and the run must
# then fail.
set -euo pipefail

format=${1:?usage: artifact-store-interlock.sh <jsonl|sqlite|mysql>}
repo=$(cd "$(dirname "$0")/../.." && pwd)
vectors="$repo/test-vectors/artifact-store"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
python_bin=${PYTHON:-python3}
inject=${INTERLOCK_INJECT:-}
read -r -a languages <<< "${LANGUAGES:?set LANGUAGES, for example LANGUAGES=\"go ts\"}"
expected_records=5

# Each language: an optional setup_<lang> (build once) and its adapter il_<lang>.
setup_go() { (cd "$repo/go" && go build -o "$work/go-interlock" ./artifact/internal/interlock); }
il_go() { "$work/go-interlock" "$@"; }
il_ts() { node "$repo/ts/scripts/artifact-interlock.mjs" "$@"; }

for lang in "${languages[@]}"; do
  if declare -F "setup_$lang" > /dev/null; then "setup_$lang"; fi
done

rows() { "$python_bin" "$repo/.github/scripts/artifact_store_rows.py" "$@"; }

# A store is a file per store for jsonl and sqlite, and a database per writing
# language for mysql. Pair copies live next to their writer's store.
target() {
  case "$format" in
    jsonl) echo "$work/$1.jsonl" ;;
    sqlite) echo "$work/$1.db" ;;
    mysql) echo "${CAPSULE_STORAGE_TEST_MYSQL:?set CAPSULE_STORAGE_TEST_MYSQL}/capsule_interlock_$2" ;;
    *) echo "unknown format $format" >&2; exit 2 ;;
  esac
}

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

last=${languages[${#languages[@]}-1]}
first=${languages[0]}

# 1. write
for lang in "${languages[@]}"; do
  store=$(target "$lang" "$lang")
  if test "$format" = mysql; then rows createdb mysql "$store"; fi
  "il_$lang" write "$format" "$store" interlock "$vectors" > "$work/write-$lang.txt"
  if test "$inject" = report && test "$lang" = "$last"; then echo tampered >> "$work/write-$lang.txt"; fi
  same "$vectors/interlock-write.txt" "$work/write-$lang.txt" "$lang write report ($format)"
done
echo "write: ${languages[*]} produce the expected report ($format)"

# 2. storage
if test "$inject" = storage; then
  store=$(target "$last" "$last")
  if test "$format" = jsonl; then printf '\n' >> "$store"; else rows tamper "$format" "$store" interlock; fi
fi
for lang in "${languages[@]}"; do
  store=$(target "$lang" "$lang")
  dump "$store" interlock > "$work/storage-$lang.txt"
  if test "$format" != jsonl; then rows schema "$format" "$store" >> "$work/storage-$lang.txt"; fi
  same "$work/storage-$first.txt" "$work/storage-$lang.txt" "$lang storage differs from $first ($format)"
done
if test "$format" = jsonl; then
  count=$(grep -c . "$work/storage-$first.txt" || true)
else
  count=$(grep -c '^capsule' "$work/storage-$first.txt" || true)
fi
if test "$count" != "$expected_records"; then
  echo "FAIL: $first stored $count records, expected $expected_records ($format)" >&2
  exit 1
fi
echo "storage: ${languages[*]} wrote identical $format data ($count records)"

# 3. read and strict, on a fresh copy of the writer's store for every pair
for reader in "${languages[@]}"; do
  for writer in "${languages[@]}"; do
    pair="$reader-$writer"
    source=$(target "$writer" "$writer")
    case "$format" in
      jsonl | sqlite)
        copy=$(target "pair-$pair" "$writer")
        ns=interlock
        cp "$source" "$copy"
        ;;
      mysql)
        copy=$source
        ns="pair-$pair"
        rows copy mysql "$source" interlock "$ns"
        ;;
    esac
    if test "$format" != jsonl; then rows created "$format" "$copy" "$ns" > "$work/created-before-$pair.txt"; fi
    for phase in read strict; do
      "il_$reader" "$phase" "$format" "$copy" "$ns" "$vectors" > "$work/$phase-$pair.txt"
      if test "$inject" = read && test "$pair" = "$last-$first"; then echo tampered >> "$work/$phase-$pair.txt"; fi
      same "$vectors/interlock-$phase.txt" "$work/$phase-$pair.txt" "$reader $phase on $writer's $format store"
    done
    dump "$copy" "$ns" > "$work/after-$pair.txt"
    same "$work/after-$first-$first.txt" "$work/after-$pair.txt" "$reader left $writer's $format store differently from $first"
    if test "$format" != jsonl; then
      rows created "$format" "$copy" "$ns" > "$work/created-after-$pair.txt"
      same "$work/created-before-$pair.txt" "$work/created-after-$pair.txt" "$reader rewrote existing rows in $writer's $format store"
    fi
  done
done
echo "read: every pair of ${languages[*]} reads, retries and purges the other's $format store alike"
