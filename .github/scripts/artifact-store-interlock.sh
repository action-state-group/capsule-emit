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
# 3. read: every language reads every language's store (all pairs): each
#    record's outcome, StorageChecksum and wire line, and retry, conflict and
#    purge outcomes against that data. All reports must equal
#    interlock-read.txt, and the reads must leave the storage unchanged.
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

target() {
  case "$format" in
    jsonl) echo "$work/$1.jsonl" ;;
    sqlite) echo "$work/$1.db" ;;
    mysql) echo "${CAPSULE_STORAGE_TEST_MYSQL:?set CAPSULE_STORAGE_TEST_MYSQL}" ;;
    *) echo "unknown format $format" >&2; exit 2 ;;
  esac
}
namespace() { if test "$format" = mysql; then echo "interlock-$1"; else echo interlock; fi; }

storage() {
  case "$format" in
    jsonl) cat "$(target "$1")" ;;
    *) "$python_bin" "$repo/.github/scripts/artifact_store_rows.py" dump "$format" "$(target "$1")" "$(namespace "$1")" ;;
  esac
}

same() {
  if ! cmp -s "$1" "$2"; then
    echo "FAIL: $3" >&2
    diff "$1" "$2" | head -20 >&2 || true
    exit 1
  fi
}

for lang in "${languages[@]}"; do
  if test "$format" = mysql; then
    "$python_bin" "$repo/.github/scripts/artifact_store_rows.py" reset mysql "$(target "$lang")" "$(namespace "$lang")"
  fi
  "il_$lang" write "$format" "$(target "$lang")" "$(namespace "$lang")" "$vectors" > "$work/write-$lang.txt"
  same "$vectors/interlock-write.txt" "$work/write-$lang.txt" "$lang write report ($format)"
done
echo "write: ${languages[*]} produce the expected report ($format)"

first=${languages[0]}
storage "$first" > "$work/storage-$first.txt"
for lang in "${languages[@]}"; do
  storage "$lang" > "$work/storage-$lang.txt"
  same "$work/storage-$first.txt" "$work/storage-$lang.txt" "$lang storage differs from $first ($format)"
done
echo "storage: ${languages[*]} wrote identical $format data"

for reader in "${languages[@]}"; do
  for writer in "${languages[@]}"; do
    "il_$reader" read "$format" "$(target "$writer")" "$(namespace "$writer")" "$vectors" > "$work/read-$reader-$writer.txt"
    same "$vectors/interlock-read.txt" "$work/read-$reader-$writer.txt" "$reader reading $writer's $format store"
  done
done
for lang in "${languages[@]}"; do
  storage "$lang" > "$work/after-$lang.txt"
  same "$work/storage-$lang.txt" "$work/after-$lang.txt" "reads changed $lang's $format storage"
done
echo "read: every pair of ${languages[*]} reads and verifies the other's $format store"
