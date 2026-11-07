# capsulectl Result records

A sealed Evidence Result capsule and the two records it cites, as `capsulectl` writes them.
`python/tests/test_result_append.py` appends them to a ledger store with `append_result`.

| File | What |
|---|---|
| `day-1.record.json`, `day-2.record.json` | Two `fyi` records sealed by `capsulectl publish` and read back with `capsulectl get --raw` (an `artifact.Record`; the payload is its one original). |
| `result-input.json` | The Evidence Result v0 document passed to `result build`. Its claims cite the two records above. |
| `result.record.json` | The sealed Result capsule, written by `capsulectl result build --capsule-out`: an `fyi`, `action_id` `evidence-result`, whose `payload` original is the canonical Result and whose references cite both records `acted_on`. |

Provenance: `capsulectl` built from action-state-group/capsule-cli at commit
`ff86686bebc2f87eb2ad648a05b98473f1e3a5e0` (v0.1.0-rc37 plus two commits), run with a throwaway
Ed25519 key (the key is not kept), the profile and commands of its README ("Result page" flow):

```
capsulectl key generate --output seed
capsulectl profile create --name rep --type sqlite --sqlite-path store.db --operator "Example Operator" \
  --signing-key-file seed --trusted-key <public key> --log-id rep-log \
  --checkpoint-signing-key-file seed --checkpoint-trusted-key <public key>
capsulectl store init --profile rep
capsulectl publish --profile rep --request day-N-req.json      # day-1, day-2: {"report":"day one"|"day two"}
capsulectl get --profile rep --capsule-id <id> --raw --output day-N.record.json
capsulectl result build --profile rep --result result-input.json --capsule-out result.record.json
```

The records are byte-for-byte as `capsulectl` wrote them. `SHA256SUMS` pins them.
