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

## Composing one action's whole story

When one action's account needs more than one part — who authorized it,
what happened, who checked — bind them into a single capsule with slot
wrappers, passed into `seal()`:

    from capsule_emit import seal, who, can, did, audit

    capsule = seal(
        who(delegation_record),                  # identity/pedigree evidence
        can(received(mandate_jws, type="...")),   # the authority/mandate — theirs, carried
        did(action_payload),                      # the action itself
    )

Each slot wrapper takes either a payload you author here (minted as its own
member capsule) or an already-sealed/carried `Capsule` (referenced as-is —
this is what makes `can(received(...))` byte-identical to calling
`received(...)` on its own). There is no other composition verb.

## Do NOT

- **Don't put raw floats in a digested payload.** They can't be reproducibly
  digested and are refused: `agent_action_capsule.canonical.FloatInDigestError`.
  Convert to a string/decimal representation first.
- **Don't pass bare `bytes`/`bytearray`/`memoryview` straight to `seal()` or
  to `who()`/`can()`/`did()`/`audit()`.** It always raises `TypeError` —
  undeclared foreign bytes are ambiguous (yours, or someone else's
  already-signed artifact?). Use `received(bytes, type="...")` for an
  already-signed foreign artifact, standalone or nested:
  `seal(received(bytes, type="..."))` or `can(received(bytes, type="..."))`.
- **Don't disable witnessing unless the integration is genuinely
  local-only.** The checkpoint/witness stream is on by default; turn it off
  with `witness=False` / `CAPSULE_WITNESS=off` only for a real
  zero-network-egress case. (The older per-capsule `anchor=` /
  `CAPSULE_ANCHOR` channel is a separate, already-off-by-default legacy path
  — leave it alone unless you specifically need it.)

## Depth

See `README.md` for the full model: may/did verdicts, chaining, the
checkpoint/witness trust ladder, and verification.
