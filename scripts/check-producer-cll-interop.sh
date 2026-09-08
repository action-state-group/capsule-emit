#!/usr/bin/env bash
set -euo pipefail

# Exercise both producers through both real CLL append/checkpoint runners. No
# witness network is used. CLL remains a test-only dependency in a temp module.
root=$(cd "$(dirname "$0")/.." && pwd)
aac_root=${AAC_REPO:-"$root/../agent-action-capsule"}
emit_ts_root=${CAPSULE_EMIT_TS_ROOT:-"$root/../capsule-emit-ts"}
cll_go_root=${CLL_GO_ROOT:-"$root/../cll-go"}
cll_ts_root=${CLL_TS_ROOT:-"$root/../cll-ts"}
python_bin=${PYTHON:-python3}
temporary=$(mktemp -d)
temporary=$(cd "$temporary" && pwd -P)
trap 'rm -rf "$temporary"' EXIT
mkdir "$temporary/driver"
cp "$root/testdata/producer-cll/main.go" "$temporary/driver/main.go"
(
  cd "$temporary/driver"
  go mod init example.invalid/producer-cll
)
(
  cd "$temporary"
  GOWORK=off go work init "$root" "$aac_root/go" "$cll_go_root" "$temporary/driver"
)
export GOWORK="$temporary/go.work"
go -C "$temporary/driver" build -o "$temporary/go-driver" .
"$temporary/go-driver" produce "$temporary/go" unused
node "$root/testdata/producer-cll/driver.mjs" "$emit_ts_root" "$cll_ts_root" produce "$temporary/ts" unused
cmp "$temporary/go.json" "$temporary/ts.json"
cmp "$temporary/go.cose" "$temporary/ts.cose"
for producer in go ts; do
  "$temporary/go-driver" checkpoint "$temporary/$producer" "$temporary/$producer-go.cose"
  node "$root/testdata/producer-cll/driver.mjs" "$emit_ts_root" "$cll_ts_root" checkpoint "$temporary/$producer" "$temporary/$producer-ts.cose"
  cmp "$temporary/$producer-go.cose" "$temporary/$producer-ts.cose"
  for log in go ts; do
    "$python_bin" "$cll_ts_root/test/interop/verify_checkpoint.py" "$temporary/$producer-$log.cose"
  done
done
printf '%s\n' 'Both producers: identical Capsule/Envelope bytes; both CLL runners: idempotent append and identical checkpoints; Python verified all four checkpoints.'
