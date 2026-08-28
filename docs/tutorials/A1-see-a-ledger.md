# A1 · See a ledger — know what your agent did

**How do you know what your agent did?**

Not what your logs *say* it did — your logs are your own word, mutable, and mean
nothing to someone who has no reason to trust your systems. The question is: is
there a record of what your agent did that *anyone* can check, without trusting
you?

That record is a **ledger** of **capsules**. Before you produce one yourself,
this page just lets you *read* two — two real ledgers from two real agent runs.
You'll see, side by side, **the run** (the model, the prompt, the tool calls and
their results — the raw material) and **the ledger that records it** (the sealed,
verifiable capsules). Then you'll verify both yourself, from the bytes alone.

> **These fixtures are synthetic and deterministic.** No model was called and no
> refund or repository read really happened — the *runs* are made up. But the
> *capsules* are genuine: produced by real `seal()` calls, with real SHA-256
> input/output digests, real Ed25519 signatures, and real content-addressed
> `capsule_id`s. They regenerate byte-for-byte identically, so everything you
> paste below reproduces exactly. Witnessing is off (`CAPSULE_WITNESS=off`) —
> these are pure, offline capsule streams.

## Set up (once)

```console
$ pip install capsule-emit==0.5.1
```

That pulls `agent-action-capsule==0.2.0` — the independent verifier — as a
dependency. The two sample ledgers live in this repo under
[`fixtures/`](fixtures/); everything below reads them straight from there.

```console
$ git clone https://github.com/action-state-group/capsule-emit
$ cd capsule-emit/docs/tutorials/fixtures
```

---

## Use case 1 — a customer-refund support agent (a write, and a refusal)

A support agent handles two refund requests. It looks up each order, checks it
against the 30-day returns policy, and issues a refund **only if it's eligible**.
One request is inside policy and the refund is dispatched; the other is outside
policy and is **refused**. The whole session is six sealed actions.

### (a) The run you hold

This is the raw material the operator keeps — it is **not** in the ledger (you'll
see why in the mapping). The full version is
[`refund-support-agent/held-run.md`](fixtures/refund-support-agent/held-run.md);
the shape of it:

- **Operator:** `northwind-retail` · **Agent:** `refund-agent@v3`
- **Model:** `anthropic / claude-sonnet-4-6`
- **System prompt:** *"You are Northwind Retail's refund support agent … issue the
  refund only if the order is eligible … Never issue a refund that fails the
  policy check."*

**Request A — "Order ORD-90287 arrived damaged. I'd like a refund."**
Delivered 6 days ago, inside the 30-day window → eligible → refund issued.

| # | Tool call (args) | Result |
|---|---|---|
| 1 | `look_up_order(order_id="ORD-90287")` | delivered, $42.00, 6 days ago |
| 2 | `check_refund_policy(order_id="ORD-90287", days_since_delivery=6, reason="item_not_as_described")` | eligible=True |
| 3 | `issue_refund(order_id="ORD-90287", amount="42.00", currency="USD")` | refund RF-55021 **dispatched** |

**Request B — "I changed my mind about order ORD-90291. Refund me."**
Delivered 85 days ago, outside the 30-day window → ineligible → **refused**.

| # | Tool call (args) | Result |
|---|---|---|
| 4 | `look_up_order(order_id="ORD-90291")` | delivered, $980.00, 85 days ago |
| 5 | `check_refund_policy(order_id="ORD-90291", days_since_delivery=85, reason="changed_mind")` | eligible=False, limit 30 days |
| 6 | `issue_refund(order_id="ORD-90291", decision="refuse")` | **refused**: outside window |

### (b) The ledger that records it

```console
$ capsule-emit ledger view refund-support-agent/ledger.jsonl

capsule ledger: refund-support-agent/ledger.jsonl  (6 record(s))

  capsule_id      actor                                       verdict       effect                chain    verify
-----------------------------------------------------------------------------------------------------------------
  205b76763d568a  refund-agent@v3                             executed      look_up_order:applie              ✓
  5d0309e26231ef  refund-agent@v3                             executed      check_refund_policy:              ✓
  d63f86d98561db  agent [↑ dispatched:5d0309e2] (confirmed)   executed      issue_refund:applied  confirms→5d0309e2…  ✓
  4b928b1329dc7b  refund-agent@v3                             executed      look_up_order:applie              ✓
  a81ba6e19294c8  refund-agent@v3                             executed      check_refund_policy:              ✓
⊛ 3a027af5b7870e  refund-agent@v3                             blocked                             sequence→a81ba6e1…  ✓
```

Six actions, six capsules — one line per consequential thing the agent did. Read
the last one: `3a027af5…` has verdict **`blocked`** and **no effect** — that's
the refusal. A refusal is a first-class record. The gate firing, and *nothing
being dispatched*, is itself the evidence that no refund went out. The `⊛` marks
it as a non-executing disposition; `sequence→a81ba6e1…` chains it to the policy
check that drove it.

Row 3 (`d63f86d9…`) is the issued refund, chained (`confirms→5d0309e2…`) to the
policy check that authorized it — *approved → executed* as one verifiable link.

### (c) The mapping — tool call ↔ capsule, args/result ↔ digests

Each tool call became exactly one capsule. Here's the raw capsule for the issued
refund (row 3), trimmed to the load-bearing fields:

```jsonc
{
  "capsule_id":  "d63f86d98561db…",          // SHA-256 of the whole capsule — the seal
  "action_id":   "issue_refund/a1c0de00…",   // the tool call, + a unique id
  "action_type": "decide",                    //   a decision that produced an effect
  "operator":    "northwind-retail",
  "developer":   "refund-agent@v3",
  "timestamp":   "2026-08-25T15:04:05Z",
  "model_attestation": {
    "provider": "anthropic",
    "model_id": "claude-sonnet-4-6",
    "compute_attestation": {
      "agent_input_digest":  "80509078e3fae3…",  // SHA-256 of the tool call's ARGS
      "agent_output_digest": "fb91aabdba545e…"   // SHA-256 of the tool call's RESULT
    }
  },
  "effect":      { "type": "issue_refund", "status": "dispatched" },
  "disposition": { "verdict_class": "executed", "human_disposed": false },
  "chain":       { "parent_capsule_id": "5d0309e26231ef…", "relation": "confirms" },
  "signature":   "d284584ca303…"              // Ed25519 signature over the content
}
```

The mapping is exact:

| In the run (raw, held) | In the capsule (sealed) |
|---|---|
| the tool called (`issue_refund`) | `action_id`, `effect.type` |
| **the args** `{order_id, amount="42.00", currency}` | `agent_input_digest` — a hash, **not the values** |
| **the result** `{refund_id: "RF-55021", …}` | `agent_output_digest` — a hash, **not the values** |
| the model that decided | `model_attestation.{provider, model_id}` |
| it was executed (vs. refused) | `disposition.verdict_class` + `effect.status` |
| it followed from the policy check | `chain.parent_capsule_id` + `relation` |

**This is the split that matters.** The capsule commits a **digest** of the args
and result — `"amount": "42.00"`, `"RF-55021"`, the customer's order — never the
raw values. Your vendor names, amounts, and order IDs stay on your machine. The
capsule proves *what they were* without containing them: reveal a raw value later
and anyone can re-hash it and check it against the committed digest. Try it —
re-hash the held args of row 3 and match `agent_input_digest`:

```console
$ python -c "import hashlib; from agent_action_capsule.canonical import jcs; \
print(hashlib.sha256(jcs({'tool':'issue_refund','order_id':'ORD-90287','amount':'42.00','currency':'USD'})).hexdigest())"
80509078e3fae36c6993379c7cf6de2187a85728d5ef74206a717412d430758d
```

That's the exact `agent_input_digest` in the capsule above. The ledger proves
**WHAT** happened; the raw stays **with you**; a stranger verifies the WHAT
**without ever seeing the raw**.

---

## Use case 2 — an on-call incident-investigation agent (read-only)

A different shape on purpose. This agent investigates why a deploy failed: it
fetches the repo, searches the logs, reads the diff, and reports a root cause
with citations. It has **no write access** — every action is a read, nothing is
dispatched, nothing is refused. Where use case 1 was *decide*, this is a stream
of *observations*.

### (a) The run you hold

Full version:
[`incident-investigation-agent/held-run.md`](fixtures/incident-investigation-agent/held-run.md).

- **Operator:** `northwind-retail` · **Agent:** `oncall-investigator@v2`
- **Model:** `anthropic / claude-opus-4-6`
- **User prompt:** *"deploy-4471 for checkout-svc failed. What happened?"*

| # | Tool call (args) | Result |
|---|---|---|
| 1 | `fetch_repo(repo="northwind/checkout-svc", ref="main")` | last deploy `deploy-4471` = failed |
| 2 | `search_logs(deploy="deploy-4471", query="level=error", window="15m")` | first error `ECONNREFUSED redis:6379` · `logs/deploy-4471#L2210` |
| 3 | `read_diff(from="deploy-4470", to="deploy-4471")` | `config/redis.yaml` host `cache-redis`→`redis` · `compare/deploy-4470...deploy-4471` |
| 4 | `summarize_findings(incident="deploy-4471-failed")` | root cause + citations; `write_performed=False` |

**Answer (held):** *the diff changed the Redis host to one that doesn't resolve;
the startup probe then fails on ECONNREFUSED. Evidence:
`logs/deploy-4471#L2210`, `compare/deploy-4470...deploy-4471`. Recommend
reverting the host change.*

### (b) The ledger that records it

```console
$ capsule-emit ledger view incident-investigation-agent/ledger.jsonl

capsule ledger: incident-investigation-agent/ledger.jsonl  (4 record(s))

  capsule_id      actor                                       verdict       effect                chain    verify
-----------------------------------------------------------------------------------------------------------------
  2e3d7361d946a3  oncall-investigator@v2                      executed                                        ✓
  1ca2cd7855f75c  oncall-investigator@v2                      executed                                        ✓
  b20d4e4e984691  oncall-investigator@v2                      executed                                        ✓
  be6e389c773d84  oncall-investigator@v2                      executed                                        ✓
```

Notice the **empty effect column**. Every capsule here is a passive observation
(`action_type` `fyi`) with **no effect record** — a read dispatches nothing, so
there is no real-world effect to claim. That's the honest shape for a read-only
run: a verifiable record of exactly what the agent *looked at* before it advised,
with no effect asserted anywhere. Compare it to use case 1, where the write
carried `issue_refund:dispatched` and a `confirms→` chain — the ledger's shape
tells you at a glance whether an agent *changed* something or only *observed* it.

### (c) The mapping

The mapping is the same discipline, minus the effect and chain:

| In the run (raw, held) | In the capsule (sealed) |
|---|---|
| the read called (`search_logs`) | `action_id` |
| **the query args** | `agent_input_digest` — a hash, not the values |
| **the log matches / findings** | `agent_output_digest` — a hash, not the values |
| the model | `model_attestation.{provider, model_id}` |
| this was a read, not a decision | `action_type: "fyi"`, no `effect` |

The evidence the agent cited (`logs/deploy-4471#L2210`, the compare URL) lives in
the held answer and in `agent_output_digest` — committed by hash. You can hand
someone the ledger to prove *the agent looked at exactly these things and wrote
nothing*, and reveal the raw findings only if and when you choose.

---

## Verify both — from the bytes alone

You've read the two ledgers. Now do what a stranger would: check them
independently, with no keys, no network, and no trust in the operator. The
verifier is a separate package (`agent-action-capsule`) on purpose — *any* tool
can produce a capsule; *any* party can verify one.

**Use case 1:**

```console
$ agent-action-capsule verify --store refund-support-agent/ledger.jsonl
Store-level verification of 6 capsule(s) in refund-support-agent/ledger.jsonl:
  [0] ok: True
  capsule_id (recomputed): 205b76763d568a16f730349d30dba6edfb1ad32316fbd9a02c7958bb27f686ca
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='look_up_order' is not a seeded effect.type value; informational, not rejected (§12)
  [1] ok: True
  capsule_id (recomputed): 5d0309e26231ef24fc33258d7f6384edb11e30bb04a6db3d56321be0b35df43d
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='check_refund_policy' is not a seeded effect.type value; informational, not rejected (§12)
  [2] ok: True
  capsule_id (recomputed): d63f86d98561db5292cd26ed9ec2ff6d69176b43c5487c63af3cf6d68f93e662
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=chained
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='issue_refund' is not a seeded effect.type value; informational, not rejected (§12)
  [3] ok: True
  capsule_id (recomputed): 4b928b1329dc7b2f16e1789d2d770c1799cd83012a3d01ce20884f253b7e8391
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='look_up_order' is not a seeded effect.type value; informational, not rejected (§12)
  [4] ok: True
  capsule_id (recomputed): a81ba6e19294c864bab08d544df5e82b397c6555bd91901492698e26f6ddbdcb
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='check_refund_policy' is not a seeded effect.type value; informational, not rejected (§12)
  [5] ok: True
  capsule_id (recomputed): 3a027af5b7870ed4ccb552ad98cfdc091f3f029cd091473b293fa47f65c927e6
  derived: effect_mode=not_applicable attestation_mode=self_attested ledger_mode=chained
  findings:
    - [info] (check 8) unknown_registry_value: chain.relation='sequence' is not a seeded chain.relation value; informational, not rejected (§12)
```

**Use case 2:**

```console
$ agent-action-capsule verify --store incident-investigation-agent/ledger.jsonl
Store-level verification of 4 capsule(s) in incident-investigation-agent/ledger.jsonl:
  [0] ok: True
  capsule_id (recomputed): 2e3d7361d946a369d16cb3d86a281c8f97c183788ef59cd0d30e9aea36963325
  derived: effect_mode=not_applicable attestation_mode=self_attested ledger_mode=standalone
  findings: none
  [1] ok: True
  capsule_id (recomputed): 1ca2cd7855f75ce271ef49c25bac1571cfbde4710599d4d084b7b4897c0fbd8a
  derived: effect_mode=not_applicable attestation_mode=self_attested ledger_mode=standalone
  findings: none
  [2] ok: True
  capsule_id (recomputed): b20d4e4e9846913b1786ca9606fcca6413b61dac9c77feb8b718cf974ff31c18
  derived: effect_mode=not_applicable attestation_mode=self_attested ledger_mode=standalone
  findings: none
  [3] ok: True
  capsule_id (recomputed): be6e389c773d840443c49897011fe007922c181b7e7965bdc43f48dd292bd205
  derived: effect_mode=not_applicable attestation_mode=self_attested ledger_mode=standalone
  findings: none
```

**Every capsule `ok: True`, zero mismatches.** The `[info]` lines are just the
verifier noting that this run uses its own action names (`look_up_order`,
`issue_refund`, …) that aren't in its seeded registry — informational, *not
rejected* (§12). `capsule_id (recomputed)` means the verifier re-hashed each
capsule from the bytes and got the same content address that's stored in it: the
seal holds.

Now break one. Change a single character in either `ledger.jsonl` — a dollar
amount, an order ID, an operator name — and run `verify` again:

```console
$ agent-action-capsule verify --store refund-support-agent/ledger.jsonl
... ok: False ...
```

The recomputed `capsule_id` no longer matches; verification fails. That mismatch
*is* the tamper-evidence. A record you can't quietly edit is a record a stranger
can trust.

## Reproduce it yourself

Both ledgers (and both held-run files) are generated by one committed,
re-runnable script — real `seal()` calls, deterministic output:

```console
$ python fixtures/generate.py
wrote docs/tutorials/fixtures/refund-support-agent/ledger.jsonl
wrote docs/tutorials/fixtures/incident-investigation-agent/ledger.jsonl
wrote docs/tutorials/fixtures/refund-support-agent/held-run.md
wrote docs/tutorials/fixtures/incident-investigation-agent/held-run.md
```

Run it twice — the files are byte-for-byte identical each time. Read
[`fixtures/generate.py`](fixtures/generate.py) to see exactly which `seal()`
arguments produced each capsule above.

---

## What you just saw

- A **ledger** is the answer to *"how do you know what your agent did?"* — one
  sealed capsule per consequential action, checkable by anyone from the bytes.
- The ledger's **shape tells the story**: a write carries an `effect` and often a
  `confirms→` chain; a refusal is a `blocked` capsule with no effect; a read-only
  run is a stream of `fyi` observations with no effect at all.
- The **digest/raw split**: the capsule commits *digests* of your inputs and
  outputs — the ledger proves **what** happened, the raw values stay **with
  you**, and a stranger verifies the **what** without ever seeing the raw.

**Next:**

- **A2 · Verify a ledger someone handed you** — you did this above; A2 goes deep
  on what each check means, and on verifying without trusting the operator, the
  clock, or the network.
- **A3 · Report an incident via a capsule ledger** — hand a counterparty a chain
  of capsules as proof of what your agent did (and didn't), revealing raw values
  only where you choose.

Ready to produce your own? → [Your first capsule](01-your-first-capsule.md).
