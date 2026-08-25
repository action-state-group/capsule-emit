# AGENTS.md

`capsule-emit` gives an AI agent a tamper-evident, independently-**witnessed**
record of what it did — so "did your agent really do that, and was it
authorized?" has a checkable answer, not just your word.

## Install

    pip install capsule-emit

## Canonical first snippet

    from capsule_emit import seal
    capsule = seal(payload)   # payload: any JSON-serializable value

`capsule.capsule_id` is the content address; `capsule.signature` is a
self-attested Ed25519 signature (always present, no opt-out). Every call also
appends to a local JSONL ledger and, by default, feeds an async, digest-only
witness (checkpoint) stream — no code required beyond `seal()`.

## Do NOT

- **Don't put raw floats in a digested payload.** They can't be reproducibly
  digested and are refused: `agent_action_capsule.canonical.FloatInDigestError`.
  Convert to a string/decimal representation first.
- **Don't pass bare `bytes`/`bytearray`/`memoryview` straight to `seal()`.**
  It always raises `TypeError` — undeclared foreign bytes are ambiguous
  (yours, or someone else's already-signed artifact?). Use
  `received(bytes, type="...")` for an already-signed foreign artifact (the
  older `carry(bytes)` still works too), then optionally
  `seal(received(bytes, type="..."))`.
- **Don't disable witnessing unless the integration is genuinely
  local-only.** The checkpoint/witness stream is on by default; turn it off
  with `witness=False` / `CAPSULE_WITNESS=off` only for a real
  zero-network-egress case. (The older per-capsule `anchor=` /
  `CAPSULE_ANCHOR` channel is a separate, already-off-by-default legacy path
  — leave it alone unless you specifically need it.)

## Depth

See `README.md` for the full model: may/did verdicts, chaining, the
checkpoint/witness trust ladder, and verification.
