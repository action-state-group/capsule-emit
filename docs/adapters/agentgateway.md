# agentgateway extension — `CapsuleEmitServicer` (ExtMcp processor)

Your gateway's access log tells *you* what passed through. A capsule turns
each `tools/call` agentgateway routes into a record built for **someone who
doesn't already trust you** — your customer, their CISO, an auditor, the other
side of a deal — including the call a guardrail refused upstream and the call
the backend answered with an error.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Logs answer "what happened?" for the
team that owns the log; they don't answer "can a stranger confirm this months
later?", because the party that ran the gateway also holds and can rewrite the
log. A capsule is content-addressed and checkable against the capsule format
([`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/), an individual
IETF Internet-Draft, not a WG document) by anyone holding the file. Altering a
record's content changes its id; an accepted external checkpoint, below, gives
an outside party a reference to check those ids against.

## What you get, in three claims

1. **The commitment and its outcome are both in the record — and a pairing is
   asserted only when it is unambiguous.** The processor rides agentgateway's
   `mcpGuardrails` seam as a remote ExtMcp gRPC service: `CheckRequest` seals a
   `planned` record the moment the gateway shows it the call — before any later
   processor, the upstream server, or the transport gets a say — and
   `CheckResponse` chains the outcome: a normal result seals `confirmed`; a
   JSON-RPC error or `isError: true` seals `failed` (the outcome at the gateway
   boundary, not the state of the world). A call that never gets a response —
   refused by a processor listed after this one, dropped upstream, gateway
   restarted — leaves its `planned` record as the evidence of the attempt. The
   proto carries no per-call id, so pairing is by order and only *asserted* when
   one plan is pending (`ext.agentgateway.pairing = paired`); a stale plan expires after `CAPSULE_AG_PENDING_TTL` seconds (keep it above
   your slowest tool call: a response arriving after its own plan expired, with
   one newer plan pending, would be paired to the wrong plan), and with more
   than one pending the outcome is sealed unchained as `unresolved` naming the candidate
   plans and the backends the gateway routed to — a guessed pairing would be a
   false record; an unresolved one is a true one. Only `tools/call` reaches the
   processor (the `methods` allow-list); every read-only MCP method bypasses it.
2. **Each record is addressed by the digest of its own canonical content, and
   signed.** The digest is self-consistency: anyone holding the file recomputes
   it, so a changed field changes the id and the link from the outcome to its
   record stops resolving. The producer signature proves the key named in the
   record signed that id — and nothing more until you pin the producer's key
   through a channel you already trust: the signature and key id sit outside
   the digest, so a ledger re-signed under a fresh key passes offline `verify`
   and still matches its checkpoint. What an outside party can check the key
   holder against is an accepted external checkpoint — see
   [Network behavior](#network-behavior).
3. **You re-check it offline.** `capsule-emit verify --store <ledger>.jsonl`
   recomputes every digest and outcome link and checks every producer signature it
   finds — no account, no service, no network. It runs the format's reference
   payload verifier plus the producer-envelope check; a record carrying no
   signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)), and the one check outside it
   is the ledger's checkpoint against the transparency service — see
   [Network behavior](#network-behavior).

## The 10-minute proof

The extension ships a runnable demo — no gateway binary, no Rust toolchain, no
network with the witness off: it drives the ExtMcp gRPC service with the same
`CheckRequest`/`CheckResponse` sequence agentgateway sends for every
`tools/call`, so the protocol boundary is the real thing — every record
verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[agentgateway]"
CAPSULE_WITNESS=off python examples/agentgateway-capsule/demo.py
```

You'll watch `tools/list` seal nothing, `submit_order` and `get_price` each
seal `planned → confirmed` chained pairs, and a `delete_ledger` the upstream
refused leave a `planned` record with no outcome — the refusal visible, never
mispaired. Five records, every one `VALID` under the offline composed check (digest recompute and producer signature), then a tamper test: flip one byte in
an output digest and verify fails. The demo seals to a throwaway ledger and
checks it for you. The same behavior was re-executed against the released
agentgateway **v1.5.0** binary on this package at **0.8.2** — a refused call,
both processor orderings, the pending-plan TTL, and two overlapping HTTP
sessions — with every ledger `VALID` under `capsule-emit verify`; the configs
and what each case sealed are under
[Running it against a real gateway](#running-it-against-a-real-gateway).
In your own deployment the wiring is one processor entry under `mcpGuardrails`
plus three environment variables, under [Reference](#reference) below.

## Network behavior

By default the processor runs an async **checkpoint/witness** stream: after
every 100 records, or at the next seal once 900 seconds have passed (there is no
background timer), it posts a *checkpoint* — size, root hash, timestamp; **never
capsule content** — to a witness, and prints a notice before the first attempt.
The default witness is the project's public one at
`witness.agentactioncapsule.org`; `CAPSULE_WITNESS_URL` names another. Each
checkpoint the witness accepts gives an outside party a reference to check the
ledger against — as far as that party trusts the witness to be independent of
the key holder — and that comparison is the one check outside offline `verify`.
Records sealed after the last accepted checkpoint are not yet committed outside
your environment, and dropping records from the end of the ledger is invisible
to offline `verify` until the next checkpoint lands. There is no final
checkpoint at exit: the exit hook only waits for one already in flight, so a run
that seals fewer than 100 records, all within 900 seconds, posts none.
Multi-witness bundling, which the default does not do for you, is what raises
the bar against a dishonest witness. For a first local run with **zero egress**,
set `CAPSULE_WITNESS=off` — offline `verify` checks the same things either way;
what you turn off is the outside reference. `CAPSULE_OPERATOR` and
`CAPSULE_DEVELOPER` seal into the
hash-chained record permanently — use a role/version tag, not personal data — and so does the `subject` you map from `jwt.sub` through `metadata`, which in
most identity providers is a stable personal identifier: it seals permanently
with no erasure path, so map a pseudonymous claim if your provider issues one. Tool
arguments and results are sealed as digests: data minimization, not
confidentiality — a digest of a low-entropy value can be recovered by
enumeration. The caller's `authorization` header never reaches the processor
when you list it under `requestHeaders.disallowed`, as the reference config
does.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The processor sees only the methods you list (today `tools/call`) and only
  from its position in the processor order: a refusal by a processor listed
  *before* it never reaches it — see [Pairing design note](#pairing-design-note).
  Closing that is a separate consistency check against the gateway's own
  access log, which is what `ext.agentgateway.backends` is sealed for.
- **Pairing is by order, and it says so.** Two overlapping sessions yield
  `unresolved` and `unmatched` outcomes with the candidates named, never a
  false chain; pairing resumes when the overlap ends. A per-call identifier in
  the proto would make them paired.
- **Provenance assumes the caller is the gateway; nothing on the wire proves
  it.** The service listens plaintext on all interfaces by default
  (`add_insecure_port`), so anyone who can reach the port can seal records with
  any `subject`. Bind it to the gateway's network and put it behind network
  policy or mTLS before you show the ledger to a stranger.
- **It records; it never refuses.** The processor answers every check with
  `Pass`; deny belongs to the guardrail you list next to it (`failureMode`
  decides what happens when *this* service is unreachable).
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire
  chain offline — an accepted witness checkpoint is the outside reference for
  that, and why the witness defaults on.
- **Kinds of `verify` — don't conflate them.** Two checks live inside
  `capsule-emit verify` — the digest recompute and the producer signature — and
  both run offline. Checking the ledger's checkpoint against the transparency
  service is outside it, and that comparison is the outside check described
  under Network behavior.
  Never quote a green `capsule-emit verify` as witness verification, and never
  read a valid signature as a name: it proves the key in the record signed it,
  not who holds the key — a ledger re-signed under a fresh key passes it, and
  so does one with the signatures stripped, unless you pass `--require-signature`
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via agentgateway's integrations or observability guides: this sits *next
  to* the gateway's OpenTelemetry traces and access log as the evidence layer,
  it doesn't replace them.)

---

## Reference

`CapsuleEmitServicer` is the foundation of this extension. agentgateway (an AAIF —
Agentic AI Foundation — project) is a high-performance Rust proxy for MCP, A2A, LLM, REST, and gRPC traffic. Its native `mcpGuardrails` hook lets an external Python gRPC service inspect or audit every MCP call before it reaches the upstream server — this is where capsule-emit plugs in.

## How it works

```
LLM agent
  ↓  MCP tools/call
agentgateway (Rust proxy, port 3000)
  ↓  mcpGuardrails gRPC CheckRequest  → capsule-emit ExtMcp service (port 50051): PLANNED capsule sealed
  ↓  forwards to upstream MCP server
  ↑  response from upstream
  ↑  mcpGuardrails gRPC CheckResponse → capsule-emit: CONFIRMED (or FAILED) capsule sealed, chained to the planned one
  ↑  response to LLM agent
```

**Two records per call.** The planned capsule is sealed the moment the gateway shows the
call to this processor — before any later processor, the upstream server, or the transport
gets a say. The outcome capsule is sealed from the response: `effect.status="confirmed"`
for a normal result, `verdict="errored"` / `effect.status="failed"` when the result is a
JSON-RPC error or carries `isError: true`; either way it `confirms`-chains to the planned
capsule. A call that never produces a response — rejected by a guardrail processor listed
after this one, an upstream transport error, a gateway restart — leaves its planned capsule
as the record of the attempt instead of nothing. Same shape as the LangChain listener.

**Consequential vs. read-only filter** is handled at the gateway config layer: only `tools/call` is listed in `methods`, so `tools/list`, `resources/read`, and every other read-only MCP method bypass the hook entirely — they never reach capsule-emit.

> **Allow-list, not deny-all.** The `methods:` config is an explicit allow-list:
> methods not listed pass through un-sealed. For current MCP this is safe —
> `tools/call` is the only method that executes tool logic and mutates external
> state. A future MCP method that is consequential would be silently unsealed
> unless you add it to `methods:`. Review this list when the MCP spec adds new
> methods. For the Signal 1 (command/query) rationale see
> [whats-consequential.md](../whats-consequential.md).

## Install

```sh
pip install "capsule-emit[agentgateway]"
```

## Quick start

**1. Start the capsule-emit ExtMcp gRPC service:**

```sh
# Env vars for your deployment
export CAPSULE_LEDGER=/var/log/capsules.jsonl
export CAPSULE_OPERATOR=acme-co
export CAPSULE_DEVELOPER=agentgateway-agent@v1

capsule-emit-agentgateway        # listens on :50051 by default
# or: python -m capsule_emit.adapters.agentgateway
# or: CAPSULE_PORT=50051 capsule-emit-agentgateway
```

**2. Configure agentgateway to call it (`config.yaml`):**

```yaml
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
binds:
  - port: 3000
    listeners:
      - routes:
          - policies:
              mcpGuardrails:
                processors:
                  - kind: remote
                    host: "localhost:50051"   # capsule-emit ExtMcp gRPC service
                    methods:
                      "tools/call": full      # request + response for tool calls ONLY
                    failureMode: failOpen     # capsule outage degrades gracefully
            backends:
              - mcp:
                  targets:
                    - name: your-mcp-server
                      stdio:
                        cmd: npx
                        args: ["@modelcontextprotocol/server-everything"]
```

**3. Start agentgateway:**

```sh
agentgateway -f config.yaml
```

Every `tools/call` routed through agentgateway now seals a planned capsule and an outcome capsule chained to it. `tools/list`, `resources/read`, and all other non-call methods produce no capsule.

## Verify a session

```sh
capsule-emit verify --store /var/log/capsules.jsonl
```

Or inspect the last 10 capsules:

```sh
capsule-emit ledger --store /var/log/capsules.jsonl --limit 10
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CAPSULE_LEDGER` | `ledger.jsonl` | Path to JSONL ledger file |
| `CAPSULE_OPERATOR` | `agentgateway-user` | Tenant / org identifier stamped on every capsule |
| `CAPSULE_DEVELOPER` | `agentgateway-agent@v1` | Agent name + version |
| `CAPSULE_PORT` | `50051` | gRPC server port |
| `CAPSULE_AG_AUDIT_KEYS` | *(unset)* | JSON object remapping audit slots to `metadata_context` keys (agentgateway#3042) |
| `CAPSULE_AG_PENDING_TTL` | `120` | Seconds a planned call waits for its response before it is expired from pairing |

## Pairing design note

agentgateway's `CheckRequest` and `CheckResponse` are separate gRPC calls with no shared
call ID in the proto, so the planned and outcome capsules are paired by order — and the
pairing is only ever *asserted* when it is unambiguous:

- **one planned call pending** → the outcome is chained to it
  (`compute_attestation["ext.agentgateway.pairing"] = {"status": "paired"}`);
- a pending planned call older than `CAPSULE_AG_PENDING_TTL` is expired first (its planned
  capsule is the record of it), so one dropped response cannot shift every later pairing by
  one;
- **more than one pending** after expiry — concurrent HTTP sessions, or a rejected call whose
  response never came and has not yet expired — → the outcome is sealed **unchained** under
  `action` `unknown`, with `pairing = {"status": "unresolved", "pending_depth": n,
  "candidates": [planned capsule ids]}`, and every pending entry is cleared so the next call
  pairs cleanly. A guessed pairing is a false record; an unresolved one is a true one, and the
  candidates plus `ext.agentgateway.backends` (the `service_names` the gateway routed to) let
  an auditor resolve it against the gateway's own access log.
- a response with nothing pending → sealed unchained, `pairing = {"status": "unmatched"}`.

**Processor order matters.** Processors run in the order listed and the first `Reject`
short-circuits the request phase; the response phase never runs for that call. List
capsule-emit **after** any processor that can reject a call if you want every outcome
chained (the refusal is then invisible to the ledger); list it **first** if you want the
refused attempt on record as a planned capsule — at the cost that the *next* response in
that session is sealed unresolved unless the TTL has cleared the orphan. For concurrent
HTTP sessions with overlapping tool calls, the honest answer today is unresolved outcomes;
a per-call identifier in the proto would make them paired.

## Or tell your coding agent

> Add the capsule-emit agentgateway extension to our mcpGuardrails config so every tools/call is sealed as an Agent Action Capsule.

## failureMode options

| Mode | Behavior when capsule-emit is unreachable |
|---|---|
| `failOpen` | Tool call continues; no capsule sealed. Use for observability-only deployments. |
| `failClosed` | Tool call is rejected with a policy error. Use when the audit trail is a hard requirement. |

## Integration surface

capsule-emit implements the `agentgateway.dev.ext_mcp.ExtMcp` gRPC service, defined in agentgateway's `ext_mcp.proto`. The Python stubs (`capsule_emit/adapters/ext_mcp_pb2.py`) are committed to the repo and require only `grpcio>=1.60` at runtime.

## Running it against a real gateway

_Executed 2026-09-14 on 0.8.1; re-executed 2026-09-17 on the released 0.8.2._

**Re-execution on the released 0.8.2 wheel (2026-09-17, macOS arm64, agentgateway v1.5.0,
`@modelcontextprotocol/server-everything`, their `manifests/jwt` sample key, `CAPSULE_WITNESS=off`).**
Four cases, each ledger `VALID` under `capsule-emit verify`:

| case | what was sealed |
|---|---|
| recorder listed first; `echo hello` → `echo forbidden` (denied by a second processor) → `echo world` → `no_such_tool` | 7 records: hello planned/confirmed paired; the refused call as `planned` only; the next response sealed `unresolved` (depth 2, both candidates named); `no_such_tool` planned then `errored`/`failed`, chained |
| recorder listed last, same calls | 4 records: two paired pairs; the refusal never reaches the recorder |
| recorder first, `CAPSULE_AG_PENDING_TTL=5`, refuse then wait 7 s | 5 records: the orphan expired ("never received a response within 5s; its planned capsule stands as the record"); `echo world` paired to its own plan |
| two overlapping HTTP sessions | 6 records: `unresolved` (depth 2) and `unmatched` (depth 0) outcomes with candidates and backends sealed, then the next call paired |

`backends=['everything']` and `subject=test-user` on every record. The 2026-09-14 run below was
on 0.8.1, whose adapter sealed one record per call at the response; its JWT and `metadata`
findings are unchanged by the adapter fix.

Everything below was executed on macOS arm64 against the released `agentgateway` v1.5.0
binary, this package at 0.8.1, and `@modelcontextprotocol/server-everything` started by the
gateway over stdio. The full transcript is in the engagement record; the parts that matter:

**The config that ran.** Two things are easy to get wrong and both are here on purpose:
`jwtAuth` is required for `jwt.sub` to resolve — without it the gateway drops the `metadata`
expression silently and the record reports `subject` in `absent` — and `authorization` is
named in `requestHeaders.disallowed` so the caller's credential never reaches the processor.
The issuer, audience, and `pub-key` file are agentgateway's own sample JWT setup
(`manifests/jwt/` in their repository; the matching token is `example2.key`, `sub: test-user`).

```yaml
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
gateways:
  default:
    port: 3000
routes:
- policies:
    jwtAuth:
      mode: strict
      issuer: agentgateway.dev
      audiences: [test.agentgateway.dev]
      jwks:
        # Relative to the folder the binary runs from, not the config file
        file: ./manifests/jwt/pub-key
    mcpGuardrails:
      processors:
      - kind: remote
        host: "localhost:50051"
        failureMode: failOpen
        methods:
          tools/call: full
        metadata:
          backendAuth.subject: jwt.sub
        requestHeaders:
          disallowed:
          - authorization
  backends:
  - mcp:
      targets:
      - name: everything
        stdio:
          cmd: npx
          args: ["@modelcontextprotocol/server-everything"]
```

**What the gateway and the processor did.** `jwtAuth` in `strict` mode returns `401` to a
request with no bearer token; with `example2.key` the MCP handshake completes (`202` on
`notifications/initialized`), `tools/call echo` returns `Echo: hello` unchanged, and one
record lands in `./ledger.jsonl`.

```console
$ agent-action-capsule verify --store ./ledger.jsonl
Store-level verification of 1 capsule(s) in ./ledger.jsonl:
  [0] ok: True
  capsule_id (recomputed): 89501d1ab4595a33cbd51f8607092682dea1314580dbe7c94990dcbad0f66cc5
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='echo' is not a seeded effect.type value; informational, not rejected (§12)
```

**What the record says about authority.** The validated subject arrived on both hooks and is
reported from the `response` phase; the three ID-JAG references are `absent` because
agentgateway does not expose them to CEL yet (tracked in agentgateway#3042); nothing was
`redacted`, i.e. no `metadata` expression resolved to token material.

```json
{
  "schema": "capsule-emit/agentgateway-audit/1",
  "issue": "agentgateway/agentgateway#3042",
  "fields": {
    "subject": {
      "value": "test-user",
      "key": "backendAuth.subject",
      "phase": "response"
    }
  },
  "grants": [],
  "absent": [
    "idjag_aud",
    "idjag_jti",
    "resource_token"
  ],
  "redacted": [],
  "metadata_keys": {
    "request": [
      "backendAuth.subject"
    ],
    "response": [
      "backendAuth.subject"
    ]
  }
}
```

**Tamper.** Changing one field of the sealed record (`action_id`) and re-running `verify`
returns `ok: False` with `capsule_id_mismatch`, exit 1. Note the scope: this is
self-attested, unwitnessed, and `ledger_mode=standalone` — verify proves the entries
presented are internally consistent and each signature matches its `key_id`; it does not
prove who sealed them or when, and a holder could re-seal the ledger under a fresh key.

## The refused call, against a real gateway (executed 2026-09-15)

Same binary (v1.5.0), same JWT setup, this adapter at the commit that introduced the
planned/outcome shape, with a second `mcpGuardrails` processor listed **after** capsule-emit
that rejects `echo` when the message contains `forbidden`. Calls in one session:
`echo hello` → `echo forbidden` → `echo world` → `no_such_tool`.

```console
$ tools/call echo forbidden
{"jsonrpc":"2.0","id":3,"error":{"code":-32603,"message":"denied by test policy"}}

$ agent-action-capsule verify --store ./ledger.jsonl
Store-level verification of 7 capsule(s) in ./ledger.jsonl:
  [0] ok: True  … effect_mode=not_applicable … ledger_mode=standalone   ← echo hello, planned
  [1] ok: True  … effect_mode=confirmed … ledger_mode=chained           ← echo hello, confirmed
  [2] ok: True  … effect_mode=not_applicable … ledger_mode=standalone   ← echo forbidden, planned — the refusal, on record
  [3] ok: True  … effect_mode=not_applicable … ledger_mode=standalone   ← echo world, planned
  [4] ok: True  … effect_mode=confirmed … ledger_mode=standalone        ← unresolved outcome: candidates [2], [3]
  [5] ok: True  … effect_mode=not_applicable … ledger_mode=standalone   ← no_such_tool, planned
  [6] ok: True  … effect_mode=dispatched_unconfirmed … ledger_mode=chained  ← no_such_tool, verdict=errored, effect=failed
```

Record `[2]` is what the previous adapter could not produce: the gateway never ran the
response phase for the refused call, and a seal-at-response design had nothing to write.
Record `[4]` is what it wrote instead — sealed under the refused call's arguments with the
next call's result, a verifying, false record. Now the pairing is declared unresolved and
both candidate planned ids are named. With the rejecting processor listed *before*
capsule-emit, the refused call is never shown to this processor and every remaining
outcome pairs cleanly; choose the order for what you want on record. The
`isError: true` result from the backend (`Tool no_such_tool not found`) seals as
`verdict=errored`, `effect.status=failed`, chained — it used to seal as `executed`.
