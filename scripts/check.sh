#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The local check before push (CONTRIBUTING.md). Pull requests run only a fast set of
# CI jobs; this runs, on your machine, every job a pull request ran before
# asg-agent-team-ethan#409, with the same commands and flags:
#
#   scripts/check.sh          Go (quality, coverage, race), Python 3.9 and 3.12,
#                             the inspect-ai tests, TypeScript on Node 22 and 24,
#                             Rust (test, artifact store, conformance, package),
#                             producer interoperability, clean-room install, the
#                             artifact store interlock (jsonl, sqlite, mysql),
#                             the hostname lint, the messaging guard and DCO.
#   scripts/check.sh --full   Also what only main and the nightly ran: the A2A
#                             controls against the live anchor (network GETs).
#
# It cannot run the leak lint and neutrality scan (their vocabulary is a
# repository secret) or the Rust tests on Linux; CI runs those.
#
# Needs: a running Docker daemon (MySQL 8.4 and Postgres 17 containers, and the
# TypeScript MySQL tests' own container), Go, rustup, uv (Python 3.9 and 3.12),
# git, curl and network access. It fetches Node 22 and 24, and the pinned
# agent-action-capsule and checkpointed-local-log commits the workflows name,
# into $CAPSULE_EMIT_CHECK_CACHE (default ~/.cache/capsule-emit-check).
#
# It runs every step, prints each step's result and time, and exits non-zero
# if any failed. It leaves the working tree as it found it, except for
# ts/node_modules (built for Node 24, the last one run) and git-ignored build
# output.
set -euo pipefail

usage() { echo "usage: scripts/check.sh [--full]" >&2; }
full=0
case "${1:-}" in
  "") ;;
  --full) full=1 ;;
  -h | --help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

repo=$(cd "$(dirname "$0")/.." && pwd)
cache=${CAPSULE_EMIT_CHECK_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/capsule-emit-check}
tmp=$(mktemp -d)
mkdir -p "$cache"

# Pins the workflows use; scripts/check.sh reads them so the two cannot drift.
aac_go_pin=$(sed -n 's/^ *ref: \([0-9a-f]\{40\}\).*/\1/p' "$repo/.github/workflows/go.yml" | head -n 1)
aac_ts_pin=$(sed -n 's/^export const AAC_COMMIT = "\([0-9a-f]\{40\}\)";$/\1/p' "$repo/ts/test/aac-pin.ts")
cll_pin=$(sed -n 's/^ *ref: \([0-9a-f]\{40\}\).*python\/v.*/\1/p' "$repo/.github/workflows/producer-interop.yml")
rust_toolchain=$(sed -n 's/^ *RUST_TOOLCHAIN: "\(.*\)"/\1/p' "$repo/.github/workflows/rust.yml")
for v in aac_go_pin aac_ts_pin cll_pin rust_toolchain; do
  [[ -n "${!v}" ]] || { echo "check.sh: could not read $v from the workflows" >&2; exit 2; }
done

containers=()
cleanup() {
  if ((${#containers[@]})); then docker rm -f "${containers[@]}" > /dev/null 2>&1 || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT

need() {
  local missing=0
  for c in "$@"; do
    command -v "$c" > /dev/null || { echo "check.sh: needs $c on PATH" >&2; missing=1; }
  done
  return "$missing"
}
need docker go rustup cargo uv git curl tar shasum perl
docker info > /dev/null 2>&1 || { echo "check.sh: needs a running Docker daemon" >&2; exit 2; }

# ---- shared setup ---------------------------------------------------------

# checkout NAME URL SHA: a cached checkout of one commit.
checkout() {
  local dir="$cache/$1-$3"
  if [[ ! -e "$dir/.done" ]]; then
    rm -rf "$dir"
    git init -q "$dir"
    git -C "$dir" fetch -q --depth 1 "$2" "$3"
    git -C "$dir" checkout -q FETCH_HEAD
    touch "$dir/.done"
  fi
  echo "$dir"
}

# node_home MAJOR: the latest Node MAJOR.x, as actions/setup-node resolves it.
node_home() {
  local major=$1 os arch sums file dir
  case "$(uname -s)" in Darwin) os=darwin ;; Linux) os=linux ;; *) echo "unsupported OS" >&2; return 1 ;; esac
  case "$(uname -m)" in arm64 | aarch64) arch=arm64 ;; x86_64) arch=x64 ;; *) echo "unsupported CPU" >&2; return 1 ;; esac
  sums=$(curl -fsSL "https://nodejs.org/dist/latest-v$major.x/SHASUMS256.txt")
  file=$(awk -v f="-$os-$arch.tar.gz" 'index($2, f) && $2 ~ /^node-v/ {print $2}' <<< "$sums")
  dir="$cache/${file%.tar.gz}"
  if [[ ! -x "$dir/bin/node" ]]; then
    curl -fsSL -o "$tmp/$file" "https://nodejs.org/dist/latest-v$major.x/$file"
    (cd "$tmp" && grep " $file\$" <<< "$sums" | shasum -a 256 -c - > /dev/null)
    tar -xzf "$tmp/$file" -C "$cache"
  fi
  echo "$dir"
}

# venv NAME VERSION: a fresh virtual environment, as each CI job starts with.
venv() {
  local dir="$tmp/venv-$1"
  uv venv -q --seed --python "$2" "$dir" >&2
  "$dir/bin/python" -m pip install -q --upgrade pip >&2
  echo "$dir"
}

# start_db NAME IMAGE ARGS...: starts a disposable container; prints host:port.
start_db() {
  local name="capsule-emit-check-$$-$1" image=$2
  shift 2
  docker run -d --rm --name "$name" "$@" "$image" > /dev/null
  containers+=("$name")
}
db_port() { docker port "capsule-emit-check-$$-$1" "$2" | head -n 1 | sed 's/.*://'; }
wait_db() {
  local name="capsule-emit-check-$$-$1" port=$2
  shift 2
  for _ in $(seq 1 120); do
    if docker exec "$name" "$@" > /dev/null 2>&1 && (exec 3<> "/dev/tcp/127.0.0.1/$port") 2> /dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "check.sh: $name did not become ready" >&2
  docker logs "$name" >&2 || true
  return 1
}
mysql_exec() { docker exec "capsule-emit-check-$$-mysql" mysql -h127.0.0.1 -uroot -e "$1"; }
reset_dbs() {
  mysql_exec "DROP DATABASE IF EXISTS capsule_storage_test; CREATE DATABASE capsule_storage_test"
  docker exec "capsule-emit-check-$$-postgres" psql -q -U postgres \
    -c "DROP DATABASE IF EXISTS capsule_storage_test" -c "CREATE DATABASE capsule_storage_test"
}

echo "==> setup: databases, pinned checkouts, Node 22 and 24"
start_db mysql mysql:8.4 -p 127.0.0.1::3306 \
  -e MYSQL_ALLOW_EMPTY_PASSWORD=yes -e MYSQL_DATABASE=capsule_storage_test
start_db postgres postgres:17 -p 127.0.0.1::5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=capsule_storage_test
# The interlock's server is not on UTC, as in artifact-interlock.yml.
start_db interlock mysql:8.4 -p 127.0.0.1::3306 \
  -e MYSQL_ALLOW_EMPTY_PASSWORD=yes -e TZ=Etc/GMT-5
aac_go=$(checkout agent-action-capsule https://github.com/action-state-group/agent-action-capsule.git "$aac_go_pin")
aac_ts=$(checkout agent-action-capsule https://github.com/action-state-group/agent-action-capsule.git "$aac_ts_pin")
cll=$(checkout checkpointed-local-log https://github.com/action-state-group/checkpointed-local-log.git "$cll_pin")
node_home 22 > /dev/null
node_home 24 > /dev/null
mysql_port=$(db_port mysql 3306)
pg_port=$(db_port postgres 5432)
interlock_port=$(db_port interlock 3306)
wait_db mysql "$mysql_port" mysql -h127.0.0.1 -uroot -e "SELECT 1"
wait_db postgres "$pg_port" pg_isready -h 127.0.0.1 -U postgres
wait_db interlock "$interlock_port" mysql -h127.0.0.1 -uroot -e "SELECT 1"

export GOTOOLCHAIN=${GOTOOLCHAIN:-auto}
export RUSTUP_TOOLCHAIN=$rust_toolchain
rustup toolchain install --profile minimal -c clippy,rustfmt "$rust_toolchain" > /dev/null 2>&1 ||
  rustup component add --toolchain "$rust_toolchain" clippy rustfmt

# ---- steps (each mirrors one CI job, same commands and flags) --------------

go_env() {
  cd "$repo/go"
  export CAPSULE_STORAGE_TEST_DSN="root@tcp(127.0.0.1:$mysql_port)/capsule_storage_test?parseTime=true"
  export CAPSULE_STORAGE_TEST_POSTGRES_DSN="postgres://postgres@127.0.0.1:$pg_port/capsule_storage_test?sslmode=disable"
  export AAC_REPO=$aac_go
}

# go.yml quality. CI diffs go.mod and go.sum against the commit after
# `go mod tidy`; here against the files as they were (then put back), so
# uncommitted edits still count.
go_quality() {
  cd "$repo/go"
  test -z "$(gofmt -l .)"
  cp go.mod "$tmp/go.mod" && cp go.sum "$tmp/go.sum"
  go mod tidy
  local tidy=0
  cmp go.mod "$tmp/go.mod" && cmp go.sum "$tmp/go.sum" || tidy=1
  cp "$tmp/go.mod" go.mod && cp "$tmp/go.sum" go.sum
  if ((tidy)); then echo "go.mod or go.sum is not tidy: run go mod tidy" >&2; exit 1; fi
  go vet ./...
  (cd ../test-vectors/artifact-store && shasum -a 256 -c SHA256SUMS)
}
go_test() { reset_dbs; go_env; scripts/check-coverage.sh 90.0; }
go_race() { reset_dbs; go_env; go test -race ./...; }

# python.yml test, for one Python version.
python_test() {
  local v=$1 py
  py=$(venv "py$v" "$v")
  export PATH="$py/bin:$PATH" # the tests run the package's console scripts
  reset_dbs
  export CAPSULE_STORAGE_TEST_MYSQL="127.0.0.1:$mysql_port"
  cd "$repo/python"
  if [[ "$v" == 3.12 ]]; then
    "$py/bin/pip" install -q "git+https://github.com/action-state-group/capsule-anchor.git"
  fi
  "$py/bin/pip" install -q -e ".[dev,agentgateway,langchain,agno,otel]"
  "$py/bin/python" -c "import agent_action_capsule as a;
assert a.DEFAULT_SPEC_VERSION == 'draft-mih-scitt-agent-action-capsule-05', a.DEFAULT_SPEC_VERSION;
print(a.__file__, a.DEFAULT_SPEC_VERSION)"
  (cd "$repo" && "$py/bin/python" -m ruff check --config python/pyproject.toml python .github examples commitment-conformance-vectors test-vectors docs skills rust)
  "$py/bin/python" -m pytest -q
  "$py/bin/python" -m pytest -q ../.github/test_leak_lint.py
  "$py/bin/python" -m pytest -q ../.github/test_release.py
}

# python.yml inspect-ai.
python_inspect_ai() {
  local py
  py=$(venv inspect 3.12)
  export PATH="$py/bin:$PATH"
  cd "$repo/python"
  "$py/bin/pip" install -q -e ".[dev,inspect-ai]"
  "$py/bin/python" -c "import agent_action_capsule as a;
assert a.DEFAULT_SPEC_VERSION == 'draft-mih-scitt-agent-action-capsule-05', a.DEFAULT_SPEC_VERSION;
print(a.__file__, a.DEFAULT_SPEC_VERSION)"
  "$py/bin/python" -c "import inspect_ai; print(inspect_ai.__version__)"
  "$py/bin/python" -m pytest -v -rs tests/test_inspect_ai_core.py tests/test_inspect_ai_eval_log.py tests/test_reconciliation.py
}

# ts.yml, for one Node version.
ts_test() {
  local node
  node=$("node_home" "$1")
  export PATH="$node/bin:$PATH" AAC_ROOT=$aac_ts CAPSULE_EMIT_GO_ROOT=../go ARTIFACT_MYSQL_TEST=1
  cd "$repo/ts"
  node --version
  npm ci
  npm run format:check
  npm run lint
  npm run typecheck
  # Local only: the MySQL conformance case runs against MySQL in Docker's Linux
  # VM on a Mac and takes about 14 s there, over vitest's 5 s default (it
  # passes under it on CI's Linux runners). Same tests and assertions;
  # https://github.com/action-state-group/capsule-emit/issues/485
  npm test -- --testTimeout=60000
  npm run build
  node scripts/check-packed-consumer.mjs
}

# rust.yml test.
rust_test() {
  cd "$repo/rust"
  (cd capsule-emit/tests/vectors && shasum -a 256 -c SHA256SUMS)
  cargo fmt --all --check
  cargo clippy --locked --all-targets -- -D warnings
  cargo test --locked
  cargo test --locked -p capsule-emit
}

# rust.yml artifact-store.
rust_artifact_store() {
  reset_dbs
  export CAPSULE_STORAGE_TEST_MYSQL="127.0.0.1:$mysql_port"
  cd "$repo/rust"
  cargo clippy --locked -p capsule-emit-artifact --all-targets --all-features -- -D warnings
  cargo test --locked -p capsule-emit-artifact --all-features
}

# rust.yml conformance.
rust_conformance() {
  local ref
  ref=$(venv rust-ref 3.12)
  (cd "$repo/python" && "$ref/bin/pip" install -q .)
  export AAC_PYTHON="$ref/bin/python"
  cd "$repo/rust"
  local scripts="$PWD/capsule-emit/tests/scripts" log="$tmp/conformance.log"
  : > "$log"
  run() { cargo test --locked -p capsule-emit --test "$1" -- --ignored --nocapture 2>&1 | tee -a "$log"; }
  AAC_VERIFY_LEDGER_SCRIPT="$scripts/verify_rust_ledger.py" run chain_ledger_conformance
  AAC_CHECKPOINT_VERIFY_SCRIPT="$scripts/checkpoint_invariance_check.py" run checkpoint_invariance
  AAC_SETTLEMENT_VERIFY_SCRIPT="$scripts/verify_rust_settlement.py" run settlement_conformance
  if grep -i "skipping" "$log"; then
    echo "a conformance test skipped itself" >&2
    exit 1
  fi
}

# rust.yml package.
rust_package() {
  cd "$repo/rust"
  python3 ../.github/scripts/check_crate_metadata.py capsule-emit/Cargo.toml capsule-emit-artifact/Cargo.toml
  cargo package --list -p capsule-emit
  cargo package --list -p capsule-emit-artifact
  cargo publish --locked --dry-run -p capsule-emit-artifact
  cargo publish --dry-run -p capsule-emit
}

# producer-interop.yml (main and nightly in CI). The vector generator rewrites
# committed vectors under go/testdata; they are restored afterwards.
producer_interop() {
  local py node24_home
  # Not local: the EXIT trap that restores them runs after this function returns.
  v04="go/testdata/capsule-emit/format4-interop" v05="go/testdata/capsule-emit/format4-interop-v05"
  py=$(venv interop 3.12)
  node24_home=$(node_home 24)
  cd "$repo"
  mkdir -p "$tmp/vectors"
  cp -a "$v04" "$tmp/vectors/v04" && cp -a "$v05" "$tmp/vectors/v05"
  # shellcheck disable=SC2329 # called by the EXIT trap
  restore() { rm -rf "${repo:?}/$v04" "${repo:?}/$v05"; cp -a "$tmp/vectors/v04" "$repo/$v04"; cp -a "$tmp/vectors/v05" "$repo/$v05"; }
  trap restore EXIT
  export PATH="$py/bin:$node24_home/bin:$PATH"
  export AAC_REPO=$aac_go CAPSULE_EMIT_TS_ROOT="$repo/ts" CAPSULE_EMIT_GO_ROOT="$repo/go"
  export CLL_GO_ROOT="$cll/go" CLL_TS_ROOT="$cll/ts"
  python -m pip install -q -e "$aac_go/python"
  python -m pip install -q -e ./python
  python -m pip install -q -e "$cll/python"
  python go/testdata/capsule-emit/format4-interop/scripts/generate_vectors.py
  (cd go && scripts/check-format4-interop.sh)
  (cd ts && npm ci && npm run test:interop)
  npm --prefix ts run build
  npm --prefix "$cll/ts" ci
  npm --prefix "$cll/ts" run build
  (cd go && scripts/check-producer-cll-interop.sh)
}

# clean-room-install.yml (main and nightly in CI). Built into a fresh
# directory instead of python/dist, which may hold older wheels locally.
clean_room() {
  local py room="$tmp/cleanroom"
  py=$(venv cleanroom-build 3.12)
  cd "$repo/python"
  "$py/bin/pip" install -q build && "$py/bin/python" -m build --outdir "$tmp/dist"
  "$py/bin/python" ../.github/scripts/check_python_package.py
  uv venv -q --seed --python 3.12 "$room" > /dev/null
  "$room/bin/pip" install -q --upgrade pip
  "$room/bin/pip" install -q "$tmp"/dist/*.whl
  "$py/bin/python" - << 'PY' > "$tmp/extras.txt"
import tomllib
d = tomllib.load(open("pyproject.toml", "rb"))
print("\n".join(d["project"].get("optional-dependencies", {})))
PY
  local whl fail=0 e
  whl=$(ls "$tmp"/dist/*.whl)
  while read -r e; do
    [ -z "$e" ] && continue
    if "$room/bin/pip" install --dry-run -q "${whl}[${e}]" > /dev/null 2>&1; then
      echo "  ok    [$e]"
    else
      echo "  FAILS [$e] — declared in pyproject but not installable from PyPI"
      fail=1
    fi
  done < "$tmp/extras.txt"
  ((fail == 0))
  CAPSULE_WITNESS=off LED_PATH="$tmp/led_path" "$room/bin/python" - << 'PY'
import os, tempfile
from capsule_emit import seal
led = os.environ["LEDGER"] = os.path.join(tempfile.mkdtemp(), "ledger")
seal({"vendor": "Frobozz", "total": "1240.19"}, action="write_order", operator="acme", developer="a@1", ledger=led)
seal({"status": "ok"}, action="confirm", operator="acme", developer="a@1", ledger=led)
print("seal() OK ->", led)
open(os.environ["LED_PATH"], "w").write(led)
PY
  local led out
  led=$(cat "$tmp/led_path")
  out=$("$room/bin/agent-action-capsule" verify --store "$led" 2>&1) || true
  echo "$out"
  if echo "$out" | grep -q "capsule_id_mismatch"; then echo "capsule_id_mismatch on a fresh install" >&2; exit 1; fi
  if echo "$out" | grep -qE "ok: False"; then echo "a capsule failed verification on a fresh install" >&2; exit 1; fi
  echo "clean-room OK: fresh PyPI install can seal() and verify --store"
  "$room/bin/pip" install -q "${whl}[otel]"
  CAPSULE_WITNESS=off "$room/bin/python" - << 'PY'
import os
import pathlib
import tempfile

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from capsule_emit import read_ledger
from capsule_emit.otel import CapsuleOTelSpanExporter

ledger = os.path.join(tempfile.mkdtemp(), "ledger")
exporter = CapsuleOTelSpanExporter(operator="acme", developer="dogfood@v1", ledger=ledger)
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
tracer = provider.get_tracer("capsule-emit-otel-dogfood")

with tracer.start_as_current_span("write_order") as span:
    span.set_attribute("http.request.method", "POST")
    span.set_attribute("gen_ai.provider.name", "local")
    span.set_attribute("gen_ai.input.messages", "must never reach the ledger")
provider.shutdown()

entries = list(read_ledger(ledger))
assert len(entries) == 1, f"expected exactly one sealed capsule, got {entries!r}"
raw = b"".join(p.read_bytes() for p in pathlib.Path(ledger).rglob("*") if p.is_file())
assert b"must never reach the ledger" not in raw, "never-enters content leaked into the ledger"
print("OTel dogfood OK -> sealed", entries[0]["capsule_id"])
PY
}

# artifact-interlock.yml (main and nightly in CI), every format.
artifact_interlock() {
  local py node24_home format inject
  py=$(venv interlock 3.12)
  node24_home=$(node_home 24)
  export PATH="$py/bin:$node24_home/bin:$PATH"
  export LANGUAGES="go ts python rust" CAPSULE_STORAGE_TEST_MYSQL="127.0.0.1:$interlock_port"
  cd "$repo"
  pip install -q "PyMySQL==1.2.3" -e "./python"
  (cd ts && npm ci && npm run build)
  for format in jsonl sqlite mysql; do
    echo "--- interlock ($format)"
    .github/scripts/artifact-store-interlock.sh "$format"
    for inject in report storage read; do
      if INTERLOCK_INJECT=$inject .github/scripts/artifact-store-interlock.sh "$format" > /dev/null 2>&1; then
        echo "interlock ($format) passed with INTERLOCK_INJECT=$inject" >&2
        exit 1
      fi
    done
  done
}

hostname_lint() { cd "$repo" && "$(venv lint 3.12)/bin/python" .github/hostname_lint.py .; }

messaging_guard() {
  local py
  py=$(venv guard 3.12)
  cd "$repo"
  "$py/bin/python" -m unittest discover -s .github/tests -v
  "$py/bin/python" .github/messaging_guard.py --self-test
  "$py/bin/python" .github/messaging_guard.py .
}

# dco.yml: every commit this branch adds over origin/main is signed off.
dco() {
  cd "$repo"
  local base sha missing=0
  base=$(git merge-base HEAD origin/main)
  while IFS= read -r sha; do
    if ! git log --format=%B -n 1 "$sha" | grep -qiE "^Signed-off-by: .+ <.+@.+>"; then
      echo "missing DCO sign-off: $(git log --format='%h %s' -n 1 "$sha")" >&2
      missing=1
    fi
  done < <(git rev-list "$base..HEAD")
  ((missing == 0))
}

# a2a-controls.yml (nightly in CI; --full only): GETs against the live anchor.
a2a_controls() { cd "$repo" && "$(venv a2a 3.12)/bin/python" examples/a2a-ap2/boundary-seal/check_controls.py; }

# ---- run --------------------------------------------------------------------

failed=()
summary=()
step() {
  local name=$1 start=$SECONDS rc
  shift
  echo
  echo "==> $name"
  set +e
  (
    set -euo pipefail
    "$@"
  )
  rc=$?
  set -e
  if ((rc == 0)); then
    summary+=("ok    $name ($((SECONDS - start))s)")
  else
    summary+=("FAIL  $name ($((SECONDS - start))s, exit $rc)")
    failed+=("$name")
  fi
}

step "dco" dco
step "hostname lint" hostname_lint
step "messaging guard" messaging_guard
step "go quality" go_quality
step "go test (coverage)" go_test
step "go race" go_race
step "python 3.9" python_test 3.9
step "python 3.12" python_test 3.12
step "python inspect-ai" python_inspect_ai
step "rust test" rust_test
step "rust artifact store" rust_artifact_store
step "rust conformance" rust_conformance
step "rust package" rust_package
step "producer interoperability" producer_interop
step "clean-room install" clean_room
step "artifact store interlock" artifact_interlock
# Node 24 last, so ts/node_modules is left built for the Node CI uses.
step "typescript (node 22)" ts_test 22
step "typescript (node 24)" ts_test 24
if ((full)); then
  step "a2a controls (live anchor)" a2a_controls
fi

echo
echo "==> summary ($SECONDS s)"
printf '%s\n' "${summary[@]}"
if ((${#failed[@]})); then
  echo "check.sh: ${#failed[@]} step(s) failed" >&2
  exit 1
fi
echo "check.sh: all steps passed"
