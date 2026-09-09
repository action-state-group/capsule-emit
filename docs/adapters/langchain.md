# LangChain adapter — `LangChainCapsuleListener`

Your LangChain callbacks tell *you* what your agent did. A capsule turns each
tool call into a record built for **someone who doesn't already trust you** —
your customer, their CISO, an auditor, the other side of a deal — including the
calls the agent tried to make and didn't complete.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Traces answer "what happened?" for the
team that owns the trace; they don't answer "can a stranger confirm this months
later?", because the party that ran the agent also holds and can rewrite the
trace. A capsule is content-addressed and independently checkable, so it can.

## What you get, in three claims

1. **The attempt and its outcome are both in the record — including the ones
   that didn't go through.** Each tool call seals a `planned` record and chains
   the outcome to it. A guard that *raises* to block a call seals a `failed`
   record (*"proposed, did not happen"*); a guard that *returns* a denial seals
   `confirmed` with the denial in the recorded output. Either way the decision is
   in the record, not just your logs.
2. **Each record is addressed by the digest of its own bytes.** Change one byte
   and the id changes and the chain link stops resolving — so anyone *other than
   the ledger's holder* is caught by arithmetic, not policy. The holder, who
   could re-seal the whole chain, is caught by the external anchor — see
   [Network behavior](#network-behavior).
3. **You re-check it offline.** `capsule-emit verify --store <ledger>.jsonl`
   recomputes the whole chain — no account, no service, no network. That is the
   chain/structure check; the separate producer-signature check is under
   [Verification](#verification).

## The 10-minute proof

The adapter ships a runnable demo — a hermetic local transparency stub (no
external network), real `langchain-core` tools, a call that succeeds and one
that's refused, every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[langchain]"
python examples/langchain-listener/demo.py
```

You'll watch it seal `planned → confirmed` for the call that works and
`planned → failed` for the one that raises, each record `PASS` on offline
verify, and a fail-closed `capsule-emit evidence` render. The demo seals to a
throwaway ledger and checks it for you; the step-by-step version of that code —
and how to keep a ledger of your own — is under [Example](#example) below.

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
pip install "capsule-emit[langchain]>=0.7.0"
```

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

    <Note>
    Float tool arguments chain correctly as of 0.7.0 — they are canonicalized
    to RFC 8785 decimal strings before digesting (#128, fixed by #135). On
    releases before 0.7.0, a float argument silently dropped the planned
    capsule and left the outcome unchained.
    </Note>
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
