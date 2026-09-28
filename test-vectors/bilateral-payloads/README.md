# Bilateral signed-payload vectors

Pins the exact bytes `capsule_emit.bilateral.request_payload`,
`action_payload` and `confirm_payload` return: the RFC 8785 JCS serialization
of each phase object, UTF-8 encoded.

`vectors.json` lists each case's function, arguments, phase object, the
expected bytes (`jcs_hex`) and their SHA-256. The expected values were
computed by agent-action-capsule's Go `canonical.JCS`
(`../go-oracle/jcs_oracle.go`), not by capsule-emit.
`tests/test_bilateral_jcs_vectors.py` checks capsule-emit against them.

The non-ASCII cases are the ones the earlier `json.dumps(sort_keys=True)`
construction got wrong: it escaped every non-ASCII code point. ASCII-only
payloads are byte-identical under both constructions.

Regenerate:

```bash
AAC_JCS_ORACLE=/tmp/aac-jcs-oracle python test-vectors/bilateral-payloads/scripts/generate_vectors.py
```
