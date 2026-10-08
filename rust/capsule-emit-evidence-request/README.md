# capsule-emit-evidence-request

The evidence request protocol of **draft-mih-agent-evidence-request-00**, in
Rust: a requester asks a responder for evidence about a subject, under a
coverage constraint, and the interaction ends in an artifact, a signed
refusal, or the requester's own record that nothing arrived.

| Module | What |
| --- | --- |
| `request` | parse a request in its JSON or CBOR binding and check it: the six subject forms, exactly one coverage member, the optional fields; the exact refusal reason when it is not well formed |
| `digest` | the request digest: SHA-256 over the bytes as received |
| `resolve` | exact-match resolution against a responder's holdings (the `Responder` trait): the anchor to serve under, or the most specific refusal reason |
| `refusal` | sign a refusal; check a received one (signature, registry and conformance, reported separately) |
| `outcome` | the one state an interaction is recorded as; a signed refusal is never an absence, and silence is never a refusal |
| `invariance` | caller invariance: the same subject under the same anchor gives the same bytes |
| `retention` | retention commitments and the breach table |
| `receipt` (0.0.3) | check RFC 9162 Ed25519 receipt form and authenticate any matching inclusion proof under a pinned service key |
| `answer` (0.0.3) | build the artifact response over a checkpointed local log (`cll`): records with inclusion proofs, a range or the full history with a range proof, the checkpoints, or the `history_card/1` derivation with consistency proofs; the artifact is deterministic and the same for every requester, the envelope is signed by the responder and binds the request digest and the anchor. `answer::verify` checks it all offline |

```rust
use capsule_emit_evidence_request::{digest, request, resolve};

let digest = digest::request_digest(&bytes);         // over the bytes as received
match request::parse_json(&bytes) {
    Ok(req) => match resolve::resolve(&req, &my_holdings) {
        resolve::Resolution::Artifact(anchor) => { /* build and send the artifact */ }
        resolve::Resolution::Refuse(reason) => { /* refusal::sign(&digest, reason, now, key) */ }
    },
    Err(e) => { /* refuse with e.reason() */ }
}
```

Where -00 leaves a wire shape open (subject and coverage shapes, digest
representation, the refusal's member names and signature profile), this crate
follows the readings the draft's conformance vectors document; each module
says which. The API is pre-stable (0.0.x) until a later draft fixes those
shapes.

**0.0.2 adds the answer path** (`answer`). 0.0.1 parsed, digested and
resolved requests, signed and verified refusals, and classified outcomes.

To accept a refusal as a responder's answer to your request, use
`refusal::verify_for` (it binds the responder's key and your request
digest); `refusal::check` reports shape and self-consistency only and is not
authentication. To accept an artifact, use `answer::verify`: it checks the
envelope signature and bindings, the artifact digest, the anchor checkpoint
(the responder's; exactly the pinned one under `expected_pin`), the coverage
constraint, every record against its digest (with your evidence format's
digest function) and every proof. Which records a `correlation` or
`exchange` subject should include is format-specific: `verify` proves each
served record is in the log under the anchor, and you check its content (a
responder can still leave a record out of those subjects; completeness is
not provable from this answer alone).

In 0.0.3, `answer::verify` also takes the caller's `witness_key(ts_url)`
lookup as its final argument and checks every carried receipt.
`VerifiedAnswer::receipts` distinguishes `Verified` (authenticated under that
key), `NoKey` (well formed but not authenticated), and `Stub` (not registered).
Only `Verified` counts as independent witnessing; the caller decides how many
witnesses are required. Malformed tree/index/path shapes are refused with or
without a key.

An answer carries at most `answer::MAX_RECORDS` (10,000) records or
checkpoints: `verify` refuses a larger one before parsing anything, and
`build` takes a limit and returns `OverLimit`, which a responder answers
with a `policy_declined` refusal. Checkpoint lists start at the stream's
first checkpoint, so a prefix cannot be left out.

Not in this crate: transport, storage, and any store-and-forward or delivery
guarantee.

## Tests

`cargo test` runs the draft's conformance vectors (every corpus, every case)
from a pinned copy in `tests/vectors/`, checked against the source's own
`SHA256SUMS` first. The Apache-2.0 `scitt-proof-array/` copies also have their
checksums validated before any test reads them; they cover proof ordering and
malformed proof classification. See `tests/vectors/SOURCES.md`.

## License

Apache-2.0. The vendored vectors under `tests/vectors/evidence-request/` are
BSD-3-Clause; their license travels with them. The shared scitt-cose receipt
vectors under `tests/vectors/scitt-proof-array/` are Apache-2.0.
