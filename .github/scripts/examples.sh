#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The examples that render evidence or carry a test suite, each with what it
# installs and what it runs.
#
#   examples.sh --list            print the entry names
#   examples.sh <entry> <python>  install the entry into <python>'s environment
#                                 and run it; give each entry a fresh environment
#
# examples.yml runs one entry per matrix row; scripts/check.sh runs every entry,
# one lane each. Add an entry here and to that matrix together
# (.github/tests/test_examples_matrix.py checks that).
#
# Each demo and suite runs from a fresh temporary copy of its directory, so
# nothing it writes (ledgers, keys, traces) lands in the working tree. Each runs
# as a user would run it: no model key (the models are scripted), and the
# witness setting the example chooses itself (most demos set
# CAPSULE_WITNESS=off; the A2A suite witnesses against its own loopback server).
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)

entries=(
  agno strands llamaindex litellm openai-agents adk langchain
  msft-agent-framework nemo-guardrails goose
  aauth-capsule-interop nanda-tax-audit a2a-task-evidence
)


install() { "$py" -m pip install -q "$@"; }

# demo SCRIPT...: runs each script from a fresh copy of its directory (some
# demos write next to themselves, not to the working directory).
demo() {
  local s work
  for s in "$@"; do
    work=$(mktemp -d)
    cp -R "$repo/$(dirname "$s")/." "$work"
    echo "==> $s"
    (cd "$work" && "$py" "$(basename "$s")")
    rm -rf "$work"
  done
}

# suite DIR ARGS...: runs pytest over a fresh copy of the example DIR, which
# keeps the example's own pytest settings (its pyproject.toml).
suite() {
  local dir=$1 work
  shift
  work=$(mktemp -d)
  cp -R "$repo/$dir/." "$work"
  echo "==> pytest $dir $*"
  (cd "$work" && "$py" -m pytest -q -p no:cacheprovider "$@")
  rm -rf "$work"
}

run() {
  case "$1" in
    agno | strands | llamaindex | litellm | openai-agents | msft-agent-framework | nemo-guardrails)
      local dir
      case "$1" in
        msft-agent-framework | nemo-guardrails) dir=$1 ;;
        *) dir=$1-listener ;;
      esac
      install -e "$repo/python[$1]"
      demo "examples/$dir/demo.py"
      ;;
    adk)
      install -e "$repo/python[adk]"
      demo examples/adk-capsule/demo.py
      ;;
    langchain)
      # langgraph is in no extra; the LangGraph demo needs it.
      install -e "$repo/python[langchain]" langgraph
      demo examples/langchain-listener/demo.py examples/langchain-listener/langgraph_demo.py
      ;;
    goose)
      install -e "$repo/python[mcp]"
      demo examples/goose-capsule/contribution_demo.py
      ;;
    aauth-capsule-interop)
      install -e "$repo/python" pytest
      suite examples/aauth-capsule-interop test_bilateral.py
      ;;
    nanda-tax-audit)
      install -e "$repo/python" -e "$repo/examples/nanda-tax-audit" pytest pytest-asyncio
      suite examples/nanda-tax-audit tests
      demo examples/nanda-tax-audit/demo.py
      ;;
    a2a-task-evidence)
      install -e "$repo/python" -r "$repo/examples/a2a-task-evidence/requirements.txt" pytest
      suite examples/a2a-task-evidence
      ;;
    *)
      echo "examples.sh: no entry $1 (examples.sh --list)" >&2
      return 2
      ;;
  esac
}

if [[ "${1:-}" == --list ]]; then
  printf '%s\n' "${entries[@]}"
  exit 0
fi
if [[ $# -ne 2 ]]; then
  echo "usage: $0 --list | <entry> <python>" >&2
  exit 2
fi
# The demos and suites run from a temporary directory, so a relative
# interpreter path is made absolute first (a bare name is found on PATH).
py=$2
if [[ "$py" == */* && "$py" != /* ]]; then py=$PWD/$py; fi
run "$1"
