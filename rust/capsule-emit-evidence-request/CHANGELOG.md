# Changelog

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
