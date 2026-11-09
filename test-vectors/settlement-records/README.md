# Settlement records conformance vectors (vendored)

`cases.json` and `registry.json` are the conformance vectors for
draft-mih-agent-settlement-records-00, "Two-Party Settlement Records for Agent
Payments" (https://datatracker.ietf.org/doc/draft-mih-agent-settlement-records/),
copied byte for byte from
[agent-action-capsule](https://github.com/action-state-group/agent-action-capsule)
`vectors/settlement/` at commit `d89f4600d53fb17fcb15d28ff77723c9310d3c8e` (the `python/v0.15.0` release commit, whose `vectors/settlement` tree is also the one at `go/v0.20.1`).
They are distributed there under the BSD 3-Clause License, reproduced in `LICENSE`. The upstream
`README.md`, `manifest.json` and `SHA256SUMS` were not copied; `SHA256SUMS` here is ours, over
`cases.json`, `registry.json` and `LICENSE`.

`python/tests/test_settlement_records_vectors.py` runs `capsule_emit.settlement.verify_settlements`
over every case and requires the derived states, failures and findings to equal
each case's `expect`. `SHA256SUMS` pins the copied bytes; to update, copy both files from
a newer commit, update the commit above and regenerate `SHA256SUMS`.
