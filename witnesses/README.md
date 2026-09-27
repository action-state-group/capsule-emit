# Witness directory

`witnesses.json` lists transparency services that accept a checkpointed-local-log checkpoint and
return a receipt a verifier can check offline. The page at
[agentactioncapsule.org/witnesses](https://agentactioncapsule.org/witnesses) is generated from this
file.

It is a directory, not an endorsement:

- Rows are sorted alphabetically by `operator`, and by nothing else.
- No row is privileged. A checkpoint is "witnessed more than once" when it carries receipts from
  more than one operator, and it is the **verifier** that decides how many it requires
  (`capsule_emit.witness_bindings.verify_witnesses`, `WitnessPolicy(min_receipts=k,
  distinct_operators=True)`).
- Being listed makes no receipt count. A verifier pins the keys it has chosen to accept; a key
  printed here is a convenience for doing that, not a trust decision made for you.

## Fields

| Field | Meaning |
|---|---|
| `operator` | Who runs the service, as the operator declares it. When an operator declares no name, the service's host name. |
| `endpoint` | Base URL. Written with its binding prefix in `witness_url=` / `CAPSULE_WITNESS_URL` (see below). |
| `binding` | `cll` (`POST /checkpoints`), `rekor` (a Sigstore Rekor log, `dsse` entry), or `scrapi` (SCITT SCRAPI `POST /entries`). |
| `statement_types` | Content types of the statements the service accepts. |
| `wire_forms` | How a checkpoint is sent. |
| `grades_issued` | What the service's receipt can attest: `countersigned-observed` (the bytes existed at a time) and/or `mmr-verified` (the service checked consistency with the previous checkpoint). |
| `key_id`, `public_key_pem` | The receipt-signing key, for a verifier to pin. |
| `source` | Where the service's code is published, if it is. |
| `since` | First date the service accepted checkpoints at this endpoint. |
| `independent_of` | Operators this one declares it shares no control, keys, or infrastructure with. |

## Using more than one witness

The binding is named by the URL scheme, so the existing setting takes all three:

```bash
export CAPSULE_WITNESS_URL="https://witness.example,rekor+https://rekor.sigstore.dev,scrapi+https://ts.example"
```

Each checkpoint is sent to each witness independently. One failing never blocks the others, and a
failed one is retried from the ledger's own backlog.

## Adding a row

Open a pull request that adds one object to `witnesses.json`, using the
[witness-directory PR template](../.github/PULL_REQUEST_TEMPLATE/witness-directory.md). Keep the list
sorted by `operator`. The test `tests/test_witness_directory.py` checks the shape and the order.
