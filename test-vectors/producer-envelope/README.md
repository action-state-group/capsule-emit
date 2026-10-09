# capsule-emit producer-envelope conformance vector

The draft-04 reversal (2026-08-24) — proves capsule-emit's own COSE_Sign1
producer-envelope construction (`capsule_emit.signing.sign_producer_envelope`
/ `LocalKeypairSigner.sign_envelope`, which reuses `scitt_cose.cose_sign1
.sign_sign1`) is cross-verifiable, byte for byte, against the frozen
`agent-action-capsule` main profile (post #74/#75: draft-04 identity +
COSE_Sign1 producer envelope) — under **both** the Go reference verifier
(`go/envelope`) and the Python reference verifier
(`agent_action_capsule.producer_envelope`).

- `valid/capsule_id.txt` — the 64-char lowercase-hex Capsule ID.
- `valid/envelope.cose` — a tagged COSE_Sign1 object over the raw 32-byte
  digest of that id (`alg=-8` EdDSA, `content_type=
  application/agent-action-capsule-id`, `kid` = raw 32-byte Ed25519 public
  key, empty unprotected map — the frozen profile, verbatim).
- `valid/capsule.json` — the full capsule body the id was computed over
  (for context; not itself part of the envelope conformance check).
- `valid/expected.json` — the expected verdict + authenticated key.
- `valid-v05/` — the same four files for the `spec_version` -05 twin: the
  `valid/` body with only `spec_version` changed to
  `draft-mih-scitt-agent-action-capsule-05`. The Capsule ID (and so the
  envelope payload) differs; the envelope profile and the verdict do not.
  `valid/` shipped in v0.5.0 and is frozen byte-for-byte; the generator
  reproduces it exactly.
- `SHA256SUMS` — checksum manifest over the corpus (`valid/` lines first,
  unchanged; `valid-v05/` lines after).

Regenerate deterministically (fixed test seed — public test material, never
a production signing key — same convention as agent-action-capsule's own
`producer-envelope-vectors/`):

```bash
python test-vectors/producer-envelope/scripts/generate_vectors.py
```

Cross-verify against the Go reference verifier (manual, one-time — see
`scripts/verify_with_go.go`'s header for the module replace-directive
setup; not part of capsule-emit's CI, which has no other Go dependency):

```bash
go run test-vectors/producer-envelope/scripts/verify_with_go.go \
    test-vectors/producer-envelope/valid/capsule_id.txt \
    test-vectors/producer-envelope/valid/envelope.cose
```

`python/tests/test_producer_envelope_vectors.py` checks the checked-in corpus
against the Python reference verifier on every test run (no Go dependency
needed in CI); the Go cross-check above was run manually against
`agent-action-capsule` main to confirm this exact corpus before it was
committed — see that commit's message for the result.
Both cases were cross-verified the same way against `agent-action-capsule`
main at 439dc02 (the -05 wire): the Go (`go/envelope`, `go/verify`) and
TypeScript (`verifyProducerEnvelope`, `verifyClass1`) references accept both
envelopes and both capsule bodies, as the Python reference does.
