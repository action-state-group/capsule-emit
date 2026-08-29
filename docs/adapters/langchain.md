# LangChain adapter — `LangChainCapsuleListener`

Seal LangChain tool calls as signed, independently verifiable action records.

## Overview

`capsule-emit` seals what a LangChain agent did into signed records that a third
party can check without trusting — or contacting — the system that produced them.

## Why

Traces answer "what happened?" for the team that owns the trace. They do not
answer "can a stranger confirm this months later?", because the same party that
ran the agent also holds and can rewrite the trace.

`capsule-emit` writes an *action record* — a capsule — for each tool call: what
was about to happen, then what did happen, signed and content-addressed. Anyone
holding the records can recompute the identifiers and check the signatures
offline.

## How it works

The listener is a standard LangChain
[`BaseCallbackHandler`](https://docs.langchain.com/oss/python/langchain/callbacks). Attach it to any
`invoke()` and it seals the tool lifecycle:

| LangChain callback | Capsule |
|---|---|
| `on_tool_start` | `effect.status = "planned"` |
| `on_tool_end` | `effect.status = "confirmed"`, chained to the planned capsule |
| `on_tool_error` | `verdict = "errored"`, `effect.status = "failed"`, chained |
| root chain start/end/error | lifecycle capsules (`include_lifecycle`, default on) |

**The two-record chain is the point.** The `planned` capsule is written *before*
the tool runs; the `confirmed` capsule is written after and carries
`chain: {parent_capsule_id: ..., relation: "confirms"}` pointing back at it. A
record of an intent that has no confirmation, or a confirmation with no prior
intent, is visible as such to anyone reading the ledger.

LLM call events are **not** sealed by default (`include_llm=False`) — token
traffic is volume, not evidence. The model identity is still captured onto the
tool capsules.

## Prerequisites

```shell
pip install "capsule-emit[langchain]==0.5.1"
```

<Note>
`capsule_emit.__version__` reads `0.5.0` in the 0.5.1 release. Check the
installed version with `importlib.metadata.version("capsule-emit")` instead.
</Note>

## Example

This example runs with no API key and no network — the evidence path is
exercised by a plain tool invocation, so you can confirm the behavior before
wiring it to a model.

<Steps>
  <Step title="Attach the listener">
    `operator` and `developer` are required and are stamped on every capsule.

    ```python
    import json, os, tempfile

    os.environ.setdefault("CAPSULE_WITNESS", "off")  # see Network behavior

    from langchain_core.tools import tool
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener
    from capsule_emit.verification import verify_capsule

    ledger_path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")

    listener = LangChainCapsuleListener(
        operator="acme-corp",             # tenant/org identifier
        developer="support-agent@1.4.0",  # agent name + version
        ledger=ledger_path,
        anchor=False,
    )
    ```
  </Step>
  <Step title="Run a tool with the listener attached">
    Pass the listener in `config={"callbacks": [...]}` on any `invoke()`, or
    register it globally per LangChain's callback docs.

    ```python
    @tool
    def issue_refund(order_id: str, amount_usd: str) -> str:
        """Refund an order. amount_usd is an exact decimal string, not a float."""
        return f"refunded {order_id} ${amount_usd}"

    result = issue_refund.invoke(
        {"order_id": "A-1029", "amount_usd": "42.50"},
        config={"callbacks": [listener]},
    )
    ```

    <Warning>
    A `float` tool argument cannot be sealed into a digest-bearing field. The
    planned capsule is dropped with a `RuntimeWarning`, the tool call still
    executes, and the outcome capsule seals **without a chain link** — a
    fail-open gap in the evidence, tracked as
    [#128](https://github.com/action-state-group/capsule-emit/issues/128).
    This bites schema-driven tools too: a model filling a JSON-schema `number`
    parameter hands you a float you never wrote. Until the fix lands, type
    monetary and quantity parameters as **exact decimal strings**. The gap is
    detectable after the fact: an unchained `confirmed` record is visible to
    anyone reading the ledger.
    </Warning>
  </Step>
  <Step title="Read the ledger">
    ```python
    records = [json.loads(line) for line in open(ledger_path)]

    for record in records:
        chain = record.get("chain")
        parent = chain["parent_capsule_id"][:16] + "..." if chain else "-"
        print(
            f"  {record['effect']['status']:9s} "
            f"capsule_id={record['capsule_id'][:16]}... "
            f"parent={parent} "
            f"ledger_mode={record['assurance']['ledger_mode']}"
        )
    ```

    ```text
    planned   capsule_id=d136b3ca318808f4... parent=- ledger_mode=standalone
    confirmed capsule_id=9ca0668a9120310b... parent=d136b3ca318808f4... ledger_mode=chained
    ```
  </Step>
</Steps>

## Verification

`verify_capsule` recomputes the capsule identifier and every digest from the
record's own content. Passing `store=` lets it resolve the chain link. It does
**not** check the producer signature — that is the second, separate check below.

```python
from capsule_emit.verification import verify_capsule

for record in records:
    verdict = verify_capsule(record, store=records)
    notes = ", ".join(f"{f.severity}:{f.code}" for f in verdict.findings) or "none"
    print(f"  {record['effect']['status']:9s} ok={verdict.ok} findings={notes}")
```

```text
planned   ok=True findings=info:unknown_registry_value
confirmed ok=True findings=info:unknown_registry_value
```

`verify_capsule` returns a `VerificationResult` with `.ok`, `.findings`,
`.errors` and `.assurance`. Findings are graded: the `info` finding above
reports that `effect.type="issue_refund"` is not a value seeded in the spec's
registry — informational, and explicitly not a rejection.

To check a whole ledger, `verify_store(records)` returns a **list** of
`VerificationResult` — one per record. Guard the empty case:

```python
from capsule_emit.verification import verify_store

results = verify_store(records)
assert results and all(r.ok for r in results)
```

### Checking the producer signature

Each record's `signature` field is a hex-encoded COSE_Sign1 envelope over the
capsule id. Authenticate it with the spec package's verifier:

```python
from agent_action_capsule.producer_envelope import verify_producer_envelope

for record in records:
    envelope = bytes.fromhex(record["signature"])
    sig = verify_producer_envelope(record["capsule_id"], envelope)
    print(f"  {record['effect']['status']:9s} signature_ok={sig.ok}")
```

A tampered envelope fails with `envelope_signature_invalid`; an envelope
replayed onto a different capsule fails with `envelope_payload_mismatch`. On
success the result carries the raw Ed25519 public key that signed the record —
whether that key is authorized for the stated operator is caller policy.

The two checks together are what "independently verifiable" means on this page:
`verify_capsule` proves the content is what the identifier commits to and the
chain is consistent; `verify_producer_envelope` proves who sealed it.

## Network behavior

Two channels exist, both off or content-free, and both worth knowing about
before you run this in production:

- **Witnessing** is **on by default**. Once enough ledger entries accumulate, a
  signed checkpoint — log size, root hash, timestamp, *never* capsule content —
  is POSTed to the default witness endpoint. Disable with `CAPSULE_WITNESS=off`.
- **Anchoring** is off unless enabled. When on, it is fire-and-forget by
  default: `EmitResult.anchored` reports that a **submission was made**, not
  that an anchor was confirmed. Set `anchor_wait=<seconds>` to block for a
  genuine outcome; without it, do not read the field as a receipt.

## Limits

- The signing key is generated and held locally. `assurance.attestation_mode`
  reads `self_attested`: the records are tamper-evident and independently
  checkable, but they attest that *this process* said so. They are not a
  third-party attestation of what the model actually did.
- `effect.effect_attestation` reads `runtime_claimed` — the confirmation is the
  framework's report that the tool returned, not proof of the external effect.
- Available for Python only. There is no JavaScript/TypeScript package.

## Resources

- [GitHub](https://github.com/action-state-group/capsule-emit)
- [PyPI](https://pypi.org/project/capsule-emit/)
- [Specification](https://github.com/action-state-group/agent-action-capsule)
- [Issue tracker](https://github.com/action-state-group/capsule-emit/issues)
