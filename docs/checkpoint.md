# `capsule_emit.checkpoint` — the CLL (Checkpointed Local Log) core

## Default wiring (since 0.5.0)

`capsule_emit.core.emit()` wires this in **by default** — no opt-in code
required. Every ledger you `emit()` into participates in a checkpoint/witness
stream automatically:

- Once a ledger accumulates `capsule_emit.witness.DEFAULT_CADENCE_ENTRIES`
  (100) entries since its last checkpoint, a signed peaks checkpoint over
  that ledger's MMR is built and registered with a Transparency Service —
  the same free public-good tier the per-emit anchor already uses by
  default, `witness.agentactioncapsule.org` (semantically a witness, not
  the anchor — **[currently served via `anchor.agentactioncapsule.org`;
  the `witness.` CNAME is pending]**, see `capsule_emit.checkpoint.emit.
  DEFAULT_TS_URL` / `_PENDING_CNAME_TARGETS`).
- **Multiple witnesses.** `witness_url=` (and `CAPSULE_WITNESS_URL`) accept a
  single endpoint or several — a list, or a comma-separated string — and the
  same checkpoint is registered with *every* endpoint named, independently;
  one endpoint failing never blocks the others. A single default endpoint is
  used unless you opt into more.
- **Digest-only** — the only bytes that ever cross the wire are the
  checkpoint's own SHA-256 digest (a hash of hashes; see `emit.py`'s
  `CheckpointRecord.digest()`). No capsule content, no ledger path, no
  action names, ever leave the process. Same posture as the anchor.
- **Async, fire-and-forget** — the checkpoint build (local, no network) and
  its TS registration (the only network call) run on a daemon thread; a
  cadence-crossing `emit()` call never blocks on it.
- **Lazy** — nothing above is imported or computed until a checkpoint is
  actually due. A caller who calls `emit()` once and exits, or whose ledger
  never crosses the cadence threshold, pays zero cost: no MMR built, no
  `capsule_emit.checkpoint` import, no network dependency touched. This is
  what keeps `import capsule_emit` (and a single below-cadence `emit()`
  call) exactly as cheap as before this default flipped on.

**First-use notice.** `capsule-emit` prints one line to stderr, once per
process, at the first `emit()`/`seal()` call where witnessing is enabled —
before the first byte ever leaves the process, not gated on a checkpoint
actually being due (the default cadence is 100 entries, so a short-lived
process might otherwise never trigger one and never see the notice). It
states what will be sent (a 32-byte digest, structurally incapable of
carrying capsule content), where (the resolved endpoint(s)), and how to turn
it off. It never prints a second time in the same process.

**Turning it off:**

```python
emit(..., witness=False)          # this call's ledger opts out
```

```bash
export CAPSULE_WITNESS=off        # opt out everywhere, no code change
```

An explicit `witness=` kwarg always overrides the env var. Repoint the
endpoint (or add more) with `emit(..., witness_url=...)` or
`CAPSULE_WITNESS_URL=…`; override the cadence with
`CAPSULE_WITNESS_CADENCE_ENTRIES=…`.

**What trust tier this reaches — be precise.** A single-TS default checkpoint
is **witnessed (single witness)**: it upgrades the stream from
*self-attested* to third-party-checkable — the witness vouches that the
records under this checkpoint **existed, in that order, and haven't been
rewritten since** (existence + order + non-deletion). It does **not** vouch
that the records' *content* is true, and it is **not** the *multi-witness,
equivocation-resistant* tier described in
[why anchoring makes it trustworthy](why-anchoring.md#be-precise-about-what-it-proves-and-doesnt)
— that tier specifically requires witnesses a verifier can cross-check: the
same checkpoint independently co-signed by, or registered to, more than one
independently-operated log. Register the default checkpoint with more than
one Transparency Service (`emit(..., witness_url=[url1, url2])` or a
comma-separated `CAPSULE_WITNESS_URL`) to climb from single-witness to
multi-witness; the zero-config default does not do this for you.

**Signing.** The default path signs checkpoints with an ephemeral,
per-process HMAC key auto-generated the first time a ledger needs one — good
enough for that ledger's own rollback/consistency self-check, but not
persisted across a process restart and not suitable for a deployment that
wants a stable, externally-attributable signing identity. Use the manual API
below with your own `Signer` for that.

## Direct / manual use

The primitives below remain independently usable, and stay opt-in in the
original sense — nothing in `capsule_emit`'s top-level import path touches
them; `import capsule_emit` alone never loads an MMR module, never pulls in
a checkpoint dependency. Reach for this directly when you want your own
cadence, your own persisted signing key, or a Transparency Service other than
the default:

```python
from capsule_emit.checkpoint import MmrLedger, CheckpointConfig, emit_checkpoint
```

## What it is

A Merkle Mountain Range (MMR) index over your own capsule ledger, plus a
signed, tamper-evident **peaks checkpoint** you can optionally register with a
Transparency Service (TS) for independent, third-party freshness evidence.

- **`core`** — the pure MMR algorithm: position math, domain-separated
  hashing, inclusion/consistency proofs. No I/O, MMRIVER-draft-compatible.
- **`store`** — an in-memory node backing (`MemoryNodeStore`).
- **`index`** — `MmrLedger`, a decorator over any object shaped like
  `LogSource` (`append`/`scan`/`fetch`/`find_gaps`/`verify`, matched
  structurally — never by importing a concrete log implementation). Wraps
  your own append-only capsule log with inclusion and range proofs.
- **`emit`** — builds, signs, and (optionally) registers a checkpoint:
  `{log_id, mmr_size, root, prev_size, prev_root, key_id, timestamp,
  signature}`, plus `witnesses` once one or more Transparency Services have
  stamped it. `key_id` doubles as a peer identifier when a deployment
  checkpoints several independent logs (e.g. one per mesh node).

## Checkpoint/stamp persistence — the stamp is a log entry, not just an in-memory field

Since 0.5.0, `capsule_emit.core.emit()`'s default wiring (`capsule_emit.witness`)
writes every checkpoint it builds — signature, `mmr_size`, and whatever
`witnesses` it collected — back into the *same ledger it covers*, as its own
JSONL line:

```json
{"kind": "checkpoint_stamp", "v": 1, "capsule_id": "<checkpoint.entry_digest()>",
 "checkpoint": {"v": 1, "kind": "mmr_checkpoint", "log_id": "...",
                "mmr_size": 100, "root": "...", "prev_size": 0,
                "prev_root": "", "key_id": "...", "timestamp": "...",
                "signature": "...", "witnesses": [...]}}
```

This is not a re-issue of any capsule and does not change any capsule's
`capsule_id` — it is a distinct entry that becomes an MMR leaf the *next*
checkpoint's `mmr.sync()` folds in. The leaf is addressed by
`CheckpointRecord.entry_digest()` — a hash over the *entire* persisted entry
(`to_dict()`: signing body, `signature`, and `witnesses` alike) — not
`CheckpointRecord.digest()` (the signing-body-only value registered with the
TS). That is what "checkpoint N's stamp is covered by checkpoint N+1" means
in practice: the history carries the evidence of its own witnessing, so
flipping or deleting a byte of a persisted stamp's `witnesses` changes its
leaf and breaks the covering checkpoint's root, rather than that evidence
living only in the `CheckpointRecord.witnesses` list of a process-local
object a restart discards. Stamp entries never wake the cadence/idle
timer — they aren't written through `core.emit()`, so they never touch
`witness.maybe_checkpoint`'s per-`emit()`-call counter.

`kind`/`v` are the entry's format-version marker: `capsule_emit.ledger.read_ledger`
filters `checkpoint_stamp` entries out by default, so every capsule-only
consumer (the CLI, `ledger.view`/`view_chains`/`show`, `server`, `permalink`,
`approval`, `holds`) keeps seeing exactly the capsule stream it always has,
unaffected by this change. Use `capsule_emit.ledger.read_ledger_entries` to
read the raw file, stamps included — that's what the checkpoint layer's own
MMR indexing (`capsule_emit.witness._JsonlLogSource.scan`) does, so stamp
entries are indexed as leaves too.

## Registration is opt-in, always — for direct/manual use of this API

`CheckpointConfig.ts_urls` defaults to an **empty list** — nothing is
registered anywhere until you set one. (This is the manual API described in
this section; `capsule_emit.core.emit()`'s own default path above does not
use `CheckpointConfig` — it resolves its endpoint the same way the anchor
does, via `witness_url=` / `CAPSULE_WITNESS_URL`.) The free public-good
witness tier at `witness.agentactioncapsule.org` (`DEFAULT_TS_URL` —
currently served via `anchor.agentactioncapsule.org`, CNAME pending) is
documented and
available, but a generated config shows it **commented out**
(`emit.EXAMPLE_CONFIG_TOML`), so opting in is an explicit uncomment. Any
conforming SCITT Transparency Service can be substituted — nothing here is
tied to one operator.

```python
from capsule_emit.checkpoint import CheckpointConfig, due_for_checkpoint, lag_exceeded

cfg = CheckpointConfig(cadence_entries=100, max_lag_entries=200)
# cfg.ts_urls == [] until you set it — e.g. cfg.ts_urls = [DEFAULT_TS_URL]
```

Cadence and scheduling are yours: `due_for_checkpoint`/`lag_exceeded` are
pure helpers over your own counter — this package never runs a timer or a
cron of its own (no timing-jitter, no scheduling as a service).

## Minimal example

```python
from capsule_emit.checkpoint import MmrLedger, emit_checkpoint

class MySigner:
    def __init__(self, key_id, secret):
        self.key_id = key_id
        self._secret = secret
    def sign(self, digest_hex: str) -> str:
        import hashlib, hmac
        return hmac.new(self._secret, digest_hex.encode(), hashlib.sha256).hexdigest()

mmr = MmrLedger(my_log)          # my_log: your own append-only capsule log
mmr.sync()                        # or append() through mmr directly
cp = emit_checkpoint(mmr, MySigner("node-a", b"..."), log_id="my-log")
# cp.root, cp.mmr_size, cp.signature -- ready to store or register.
```

## Provenance

Ported from `capsule-ledger`'s `capsule_ledger/mmr/{core,index,store}.py`
per Amendment E (2026-08-21): the CLL core is substrate a counterparty needs
in order to verify a log, so it lives in the neutral producer library rather
than forked per consumer. `capsule-ledger` consumes this package through its
public interface — see its own docs for the ledger-specific wiring.
