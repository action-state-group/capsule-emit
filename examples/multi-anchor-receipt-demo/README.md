# Multi-anchor receipt demo

Demonstrates a single Agent Action Capsule carrying registration receipts from
two independently-operated SCITT transparency logs, with a verifier that checks
both receipts separately.

## Limitation — stated plainly

Both transparency log instances in this demo are operated by the same party
running the demo.  Running two instances yourself proves the *mechanism* works;
it says nothing about *independence*.  Operator independence — the property
this mechanism exists to enable — requires two parties with separate key
material, governance, and infrastructure.  This demo does not have that.

## What this shows

1. **One record, two receipts.**  A single capsule is submitted to two
   separate SCITT transparency services.  Each service issues its own COSE
   Receipt signed with its own key.

2. **Per-receipt verification.**  The verifier checks each receipt
   independently using the log's public key saved at submission time.
   There is no aggregate result — each receipt reports its own status.

3. **Partial reachability.**  When one anchor is unreachable at submission
   time, the record carries one receipt and one "absent" marker.  The verifier
   reports each status separately.  An absent receipt is never treated as a
   pass or as a fail — it is absent.

4. **Verifier policy is a relying-party decision.**  How many receipts a
   relying party requires, and from which specific logs, is not encoded in the
   capsule format.  This demo has no opinion on that.

## Usage

```
pip install "capsule-emit[dev]" capsule-anchor scitt-cose
```

**Full run** (both anchors up, then partial-reachability scenario):
```
python examples/multi-anchor-receipt-demo/run_demo.py
```

**Partial-reachability only** (anchor-B intentionally not started):
```
python examples/multi-anchor-receipt-demo/run_demo.py --partial
```

**Verify a saved record** (produced by `run_demo.py`):
```
python examples/multi-anchor-receipt-demo/verify_demo.py /tmp/multi_receipt_record.json
```

## Files

| File | Description |
|------|-------------|
| `run_demo.py` | Starts two local anchor instances, emits a capsule, submits to both, verifies both receipts, then runs the partial-reachability scenario |
| `verify_demo.py` | Loads a saved multi-receipt JSON record and verifies each receipt independently |

## How it works

Both anchor instances are the public [`capsule-anchor`][ca] service run
locally in in-memory mode with ephemeral signing keys.

Each submission goes to `/v1/digest`, which registers the capsule digest in
the anchor's append-only Merkle log and returns a COSE Receipt
(RFC 9162 / CT-log inclusion proof).  The receipt is verifiable offline
against the log's Ed25519 public key, which is saved alongside the receipt
in the multi-receipt record.

The verifier uses `scitt_cose.verify_receipt` to check each receipt without
contacting the anchor.  If an anchor was unreachable at submission time, the
record marks that slot absent and the verifier reports it as absent.

[ca]: https://github.com/action-state-group/capsule-anchor
