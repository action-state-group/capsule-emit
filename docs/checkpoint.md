# `capsule_emit.checkpoint` — the CLL (Checkpointed Local Log) core

An **opt-in** subpackage. Nothing in `capsule_emit`'s top-level import path
touches it — `import capsule_emit` alone never loads an MMR module, never
pulls in a checkpoint dependency. You only pay for it when you ask for it:

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
  `{log_id, mmr_size, root, peaks_digest, prev_size, prev_root, key_id,
  timestamp, signature}`. `key_id` doubles as a peer identifier when a
  deployment checkpoints several independent logs (e.g. one per mesh node).

## Registration is opt-in, always

`CheckpointConfig.ts_urls` defaults to an **empty list** — nothing is
registered anywhere until you set one. The free public-good witness tier at
`anchor.agentactioncapsule.org` (`DEFAULT_TS_URL`) is documented and
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
