# Test vectors: where they come from

Copied verbatim from the source; not edited here.

| Directory | Source | Pin | License |
| --- | --- | --- | --- |
| `evidence-request/` | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `vectors/evidence-request/` (the draft-mih-agent-evidence-request-00 conformance vectors) | commit `6cd137ce2dd5329aa0af6ec06c7d1dec4454792e` | BSD-3-Clause: `evidence-request/LICENSE`, the source repository's license, copied verbatim, travels with the copies |

`evidence-request/SHA256SUMS` is the source's own checksum file, unchanged:
it lists every vector file except `LICENSE` (added here). The test suite
checks every listed file against it before it reads any vector, and CI runs
`shasum -a 256 -c SHA256SUMS` in that directory.

To update: copy the new set unchanged from the source commit, keep
`LICENSE`, and update the pin here.
