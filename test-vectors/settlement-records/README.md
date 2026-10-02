# Settlement records conformance vectors (vendored)

`cases.json` and `registry.json` are the conformance vectors for
draft-mih-agent-settlement-records-00, "Two-Party Settlement Records for Agent
Payments" (https://datatracker.ietf.org/doc/draft-mih-agent-settlement-records/),
copied byte for byte from
[agent-action-capsule](https://github.com/action-state-group/agent-action-capsule)
`vectors/settlement/` at commit `565d1f0eaab1659b537426b434cc9374a39570f2`.
They are distributed there under the BSD 3-Clause License, reproduced in `LICENSE`. The upstream
`README.md`, `manifest.json` and `SHA256SUMS` were not copied; `SHA256SUMS` here is ours, over
`cases.json`, `registry.json` and `LICENSE`.

`tests/test_settlement_records_vectors.py` runs `capsule_emit.settlement.verify_settlements`
over every case and requires the derived states, failures and findings to equal
each case's `expect`. `SHA256SUMS` pins the copied bytes; to update, copy both files from
a newer commit, update the commit above and regenerate `SHA256SUMS`.
