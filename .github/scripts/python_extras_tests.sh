#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Runs the Python tests that need one optional SDK, with that SDK installed,
# and fails if any of them skipped. The main pytest job installs none of these
# SDKs, so without this their tests never run.
#
#   python_extras_tests.sh --list            print the entry names
#   python_extras_tests.sh <entry> <python>  install the entry into <python>'s
#                                            environment and run its tests
#
# Run from python/. python.yml's extras job runs one entry per matrix row;
# scripts/check.sh --full runs every entry. Add an entry here and to that
# matrix together.
set -euo pipefail

entries=(litellm llamaindex openai-agents msft-agent-framework strands nemo-guardrails crewai langgraph)

if [[ "${1:-}" == --list ]]; then
  printf '%s\n' "${entries[@]}"
  exit 0
fi
if [[ $# -ne 2 ]]; then
  echo "usage: $0 --list | <entry> <python>" >&2
  exit 2
fi
entry=$1 py=$2

case "$entry" in
  litellm)
    # The proxy tests need litellm's proxy extra and websockets.
    install=(".[dev,litellm]" "litellm[proxy]" websockets)
    modules=(litellm litellm.proxy.proxy_server websockets)
    tests=(tests/test_litellm_listener_sdk.py tests/test_docs_litellm_sdk.py
      "tests/test_ledger_layout.py::test_litellm_names_capsule_emit_ledger_in_its_warning")
    ;;
  llamaindex)
    install=(".[dev,llamaindex]")
    modules=(llama_index.core)
    tests=(tests/test_llamaindex_listener_sdk.py)
    ;;
  openai-agents)
    install=(".[dev,openai-agents]")
    modules=(agents agents.testing)
    tests=(tests/test_openai_agents_listener.py tests/test_openai_agents_listener_sdk.py)
    ;;
  msft-agent-framework)
    install=(".[dev,msft-agent-framework]")
    modules=(agent_framework)
    tests=(tests/test_msft_agent_framework_sdk.py tests/test_docs_msft_agent_framework.py)
    ;;
  strands)
    install=(".[dev,strands]")
    modules=(strands)
    tests=(tests/test_strands_listener.py)
    ;;
  nemo-guardrails)
    install=(".[dev,nemo-guardrails]")
    modules=(nemoguardrails)
    tests=(tests/test_nemo_guardrails.py)
    ;;
  crewai)
    install=(".[dev,crewai]")
    modules=(crewai)
    tests=(tests/test_crewai_listener.py)
    ;;
  langgraph)
    # No public extra carries langgraph; it is a test-only dependency here.
    install=(".[dev,langchain]" langgraph)
    modules=(langchain_core langgraph.graph langgraph.prebuilt)
    tests=(tests/test_langchain_listener.py)
    ;;
  *)
    echo "unknown entry: $entry (see --list)" >&2
    exit 2
    ;;
esac

"$py" -m pip install -q "${install[@]}"
for m in "${modules[@]}"; do
  "$py" -c "import $m" || { echo "$entry: cannot import $m" >&2; exit 1; }
done
log=$(mktemp)
trap 'rm -f "$log"' EXIT
rc=0
"$py" -m pytest -q -rs -p no:cacheprovider "${tests[@]}" > "$log" 2>&1 || rc=$?
cat "$log"
if ((rc != 0)); then exit "$rc"; fi
if grep -q '^SKIPPED' "$log"; then
  echo "$entry: a test skipped with its SDK installed" >&2
  exit 1
fi
