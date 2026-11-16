# The evidence-commitment entry in an A2A Agent Card

An agent card can point to the node's signed `evidence-commitment/1` with one
`capabilities.extensions[]` entry. The encoding is section 5.2 of
[`evidence-commitment-carriers-v0`](https://github.com/action-state-group/agent-action-capsule/blob/main/spec/bindings/evidence-commitment-carriers-v0.md)
in agent-action-capsule; this page shows how the reference implementation in
[`examples/a2a-task-evidence`](../../examples/a2a-task-evidence/) writes and reads it. The entry is
separate from the evidence-request entry of IP section 11.4.4: a card whose evidence-request entry
carries a commitment member fails V0 (`discovery_object`). The `a2a-task-evidence/v1` extension is
unchanged.

```json
{
  "uri": "https://github.com/action-state-group/agent-action-capsule/blob/main/spec/bindings/evidence-commitment-carriers-v0.md",
  "description": "Points to this agent's signed evidence commitment; verify the commitment, not this entry",
  "required": false,
  "params": {
    "commitment_uri": "https://node.example/commitment.json",
    "commitment_digest": "sha256:<64 lowercase hex>"
  }
}
```

- `commitment_digest` is SHA-256 over the commitment octets exactly as served.
- `uri` is exactly the string shown, compared as a string and not dereferenced. `params` has exactly the
  two members shown, and `commitment_uri` is an `https:` URI.
- The entry carries no size or count, and neither does the commitment (spec section 5.1).
- `required` is `false`: a client that ignores the entry still works.
- The entry is never placed in another extension's `params`.
- A card's JWS, when present, authenticates the card; its signer is never compared with the
  commitment's `key_id` (V5).
- Publishing is off by default (spec sections 1 and 9; a responder that would not publish its history must
  not publish a commitment, since the staples are its history). The example server adds the entry only when started with
  `--commitment <file>` and `--commitment-uri <https URI>`.

## What a reader trusts

Nothing in the card. `read_commitment` in
[`commitment_entry.py`](../../examples/a2a-task-evidence/commitment_entry.py) checks the entry's form
(V0), fetches `commitment_uri`, compares the digest (V1), and verifies the octets on their own: the
profile, the Ed25519 signature over the JCS of the rest, and the staple digests (V2, V3). A digest
mismatch means the pointer is stale and is reported as such; it never rejects octets that verify. The
card's own claims (name, description and the rest) are not read; what the reader relies on comes
from the verified commitment only (V7). Key binding, newest-wins and the hand-off of stapled history cards (V4 to V6)
belong to the asker's own verifier. The `verify` argument replaces the small built-in check with a
stricter one that takes the fetched octets.

Out of scope: cards hosted by services that have no `extensions[]`.
