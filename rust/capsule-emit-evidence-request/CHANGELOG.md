# Changelog

## 0.0.3

- **`answer::verify` checks the witness receipts an answer carries** (§4.1,
  §8.1). Every receipt on the anchor and on a `checkpoints` or
  `history_card/1` list must be bound to the checkpoint it is carried on (its
  entry hash is `SHA-256(checkpoint digest)`) and must be a COSE receipt with
  an RFC 9162 inclusion proof; otherwise `VerifyError::ReceiptMalformed` or
  `VerifyError::ReceiptBinding`. `verify` takes the requester's witness trust
  policy, `witness_key(ts_url) -> Option<VerifyingKey>`: under a key, the
  receipt's proof and signature must cover that checkpoint's entry. Each
  receipt is reported in `VerifiedAnswer::receipts` as `Verified`, `NoKey`
  (bound and well formed, not authenticated) or `Stub`; only `Verified`
  counts as independent witnessing, and how many are required stays the
  requester's policy. Breaking: `verify` has a new last argument.
- **A fixed exchange-half pin has a stable anchor** (§5 fixed-pin
  invariance). An `exchange` subject pinned by the requester's own half is
  answered under the earliest checkpoint covering every checkpointed record
  that cites the half, no longer the latest, so growth that adds no citing
  record leaves the artifact unchanged. A new citing record, once a
  checkpoint covers it, advances the anchor, and the artifact lists the
  records it covers. `verify` enforces it: the anchor's own signed
  `prev_size` must not already cover the last served record
  (`VerifyError::CoverageUnmet`).
- **The refusal's CBOR binding is deterministic** (RFC 8949 §4.2.1, as §10
  requires): map keys in the bytewise order of their encodings (`sig`,
  `key_id`, `reason`, `issued_at`, `request_digest`), pinned by an exact
  wire-byte test.
- New dependencies: `coset` 0.4 and `base64` 0.22 (both already in the tree
  through `checkpointed-local-log`). `receipt` is the Ed25519 subset of the
  RFC 9162 receipt verifier in action-state-group/scitt-cose.

## 0.0.2

- `answer`: build the artifact response over a checkpointed local log
  (`checkpointed-local-log`) and verify it offline. The artifact is
  deterministic RFC 8785 JSON of (subject, anchor, derivation); the
  verification material carries the anchor checkpoint and inclusion, range
  or consistency proofs; the envelope binds the request digest, the anchor
  and the artifact digest under the responder's key.
- `answer::MAX_RECORDS` caps an answer's records or checkpoints; `verify`
  refuses a larger answer before parsing it, and `build` takes a limit
  (`BuildError::OverLimit`, answered as `policy_declined`).
- Checkpoint lists (`checkpoints`, `history_card/1`) must start at the
  stream's first checkpoint.
- New dependency: `checkpointed-local-log` 0.2.0.

## 0.0.1

- `request`, `digest`, `resolve`, `refusal` (`sign`, `verify_for`,
  `check`), `outcome`, `invariance`, `retention` for
  draft-mih-agent-evidence-request-00, tested against the draft's
  conformance vectors.
