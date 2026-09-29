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

**0.0.1 does not build answers.** It parses, digests and resolves
requests, signs and verifies refusals, and classifies outcomes. Building the
artifact (a record, a range or a chain segment with its inclusion or range
proof under the anchor) is the responder's in 0.0.1; **0.0.2 adds the answer
path**.

To accept a refusal as a responder's answer to your request, use
`refusal::verify_for` (it binds the responder's key and your request
digest). `refusal::check` reports shape and self-consistency only and is not
authentication.

Not in this crate: transport, storage, and any store-and-forward or delivery
guarantee.

## Tests

`cargo test` runs the draft's conformance vectors (every corpus, every case)
from a pinned copy in `tests/vectors/`, checked against the source's own
`SHA256SUMS` first. See `tests/vectors/SOURCES.md`.

## License

Apache-2.0. The vendored vectors under `tests/vectors/evidence-request/` are
BSD-3-Clause; their license travels with them.
