# Anchor reliability and the key story

`capsule-emit` submits a SHA-256 digest to a public SCITT transparency log on
every `emit()` call.  This document covers what happens when the log is
unreachable, how to confirm a capsule actually reached the log, and what key
is used at the anchor.

---

## failOpen: anchor failure is never silent

The anchor submission runs in a background daemon thread.  If the log is
unreachable — network partition, timeout, 5xx from the log service — the
capsule is **already sealed locally** (written to your ledger) and the calling
code continues.  The system does **not** raise an exception.

What it **does** do: emit a `logging.WARNING` on the `capsule_emit.core`
logger so the failure is never invisible:

```
WARNING capsule_emit.core: capsule-emit: anchor submission FAILED for
  4a3f1b2c… — capsule is sealed locally but NOT committed to the
  transparency log (failOpen: the action continues). Error: <reason>
```

To surface this in your application, attach a log handler to `capsule_emit` or
`capsule_emit.core`.  Any standard Python logging configuration works — file,
structured JSON, syslog.

```python
import logging
logging.getLogger("capsule_emit").addHandler(logging.StreamHandler())
logging.getLogger("capsule_emit").setLevel(logging.WARNING)
```

**What `cap.anchored` means:** `True` indicates that an anchor submission was
*started* — not that it succeeded.  If you need to assert the capsule reached
the log, use `cap.wait_receipt()`.

---

## Getting the inclusion receipt

Call `wait_receipt(timeout)` to block until the anchor returns:

```python
cap = emit(
    action="write_order",
    operator="acme-co",
    developer="po-agent@v1",
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
    agent_output=result,
)

receipt = cap.wait_receipt(timeout=10.0)
if receipt is None:
    # anchor failed or timed out — check WARNING logs
    ...
else:
    # receipt is the dict the log returned, e.g. {"ok": True, "tree_size": N}
    store_with_capsule(receipt)
```

`wait_receipt()` is idempotent: calling it a second time returns the cached
result immediately.  The receipt is also stored on `cap.receipt` after the
first successful call.

When `anchor=False` was passed, `wait_receipt()` always returns `None`
immediately (no anchor was started).

---

## What's on the log, and what isn't

The anchor endpoint receives only:

```json
{ "capsule_id": "<64-char SHA-256 hex>" }
```

Nothing else crosses the wire — no inputs, outputs, prompts, amounts, or
vendor names.  The log records the capsule's content address and the timestamp
of submission.  The raw capsule (including digests) stays on your machine in
your ledger.

---

## Digest salting: preventing cross-capsule correlation

By default (`salt_digests=True`), each `emit()` call generates a fresh random
16-byte hex salt.  The input and output digests are computed as
`SHA256(salt + "|" + json(value))` and the salt is stored in the capsule's
`compute_attestation` field as `digest_salt`.

**Why this matters:** without salting, a low-entropy input like
`{"vendor": "A", "total": 100}` produces the same digest every time.  An
adversary who knows the domain of possible inputs can precompute a rainbow
table and correlate capsules — even though the raw data never left your
machine.  A per-emit random salt makes every digest unique to its call; no
rainbow table spans capsules.

**Verifying your own capsule:** because the salt is stored in the capsule
itself, you can always recompute any digest:

```python
import hashlib, json

salt = capsule["model_attestation"]["compute_attestation"]["digest_salt"]
raw = json.dumps(agent_input, sort_keys=True, separators=(",", ":"), default=str)
digest = hashlib.sha256((salt + "|" + raw).encode()).hexdigest()
assert digest == capsule["model_attestation"]["compute_attestation"]["agent_input_digest"]
```

To disable salting (e.g. for testing digest-equality invariants or when
deterministic digests are required): `emit(..., salt_digests=False)`.

---

## What key is used at the anchor?

The public anchor at `https://anchor.agentactioncapsule.org/v1/digest` records
digest submissions against a server-held Merkle tree.  The anchor does **not**
sign individual capsules with your key — it is a public append-only log, not
a key-escrow service.

The capsule itself is content-addressed by its own SHA-256 hash (the
`capsule_id`).  Verification of the capsule's integrity uses that hash alone —
no external key is required.  Any party with the capsule bytes can verify it
offline using the spec verifier:

```bash
pip install agent-action-capsule
agent-action-capsule verify --store ./ledger.jsonl
```

---

## Self-hosting the anchor

The log service (`capsule-anchor`) is open-source and self-hostable.  To
repoint:

```python
emit(..., anchor_url="https://your-anchor.example.com/v1/digest")
# or via environment:
# AAC_ANCHOR_URL=https://your-anchor.example.com/v1/digest
```

When switching anchors, note that capsules already submitted to the old log are
still verifiable there.  The `anchor_url` stored in a capsule (if any) is
informational; verification does not require the original log to be reachable.

---

## Reliability checklist

| Concern | Answer |
|---|---|
| Anchor is down at emit time | Sealed locally, WARNING logged, action continues |
| Anchor is down for hours | All capsules sealed locally; batch-submit later (not built-in yet) |
| Confirm capsule reached log | `cap.wait_receipt(timeout=N)` — `None` → failure |
| Privacy: what leaves the machine? | Only `capsule_id` (a SHA-256 hash) |
| Input privacy against rainbow tables | Per-emit random salt on by default |
| Verify a capsule without the log | `agent-action-capsule verify` — content-address only |
| Key rotation | No per-capsule signing key in the emit tier; see gate layer |
| Offline / air-gapped | `emit(..., anchor=False)` — ledger only |
