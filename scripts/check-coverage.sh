#!/usr/bin/env bash
set -euo pipefail

threshold="${1:-90.0}"
profile="$(mktemp "${TMPDIR:-/tmp}/capsule-producer-coverage.XXXXXX")"
trap 'rm -f -- "${profile}"' EXIT

go test ./... -covermode=atomic -coverprofile="${profile}"
total="$(go tool cover -func="${profile}" | awk '/^total:/ {gsub("%", "", $3); print $3}')"

awk -v total="${total}" -v threshold="${threshold}" 'BEGIN {
    if (total + 0 < threshold + 0) {
        printf "coverage %.1f%% is below required %.1f%%\n", total, threshold > "/dev/stderr"
        exit 1
    }
    printf "coverage %.1f%% meets required %.1f%%\n", total, threshold
}'
