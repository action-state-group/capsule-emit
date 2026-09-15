# agentgateway extension

The hardened `CapsuleEmitServicer` you already know is the foundation of this extension. agentgateway (the 4th AAIF project) is a high-performance Rust proxy for MCP, A2A, LLM, REST, and gRPC traffic. Its native `mcpGuardrails` hook lets an external Python gRPC service inspect or audit every MCP call before it reaches the upstream server — this is where capsule-emit plugs in.

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
call to this processor — before any other processor, the upstream server, or the transport
gets a say. The outcome capsule is sealed from the response: `effect.status="confirmed"`
for a normal result, `verdict="errored"` / `effect.status="failed"` when the result is a
JSON-RPC error or carries `isError: true`; either way it `confirms`-chains to the planned
capsule. A call that never produces a response — rejected by a guardrail processor listed
before this one, an upstream transport error, a gateway restart — leaves its planned capsule
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
agent-action-capsule verify --store /var/log/capsules.jsonl
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

## Run the demo

```sh
pip install "capsule-emit[agentgateway,dev]"
python examples/agentgateway-capsule/demo.py
```

Expected output:

```
============================================================
agentgateway capsule demo — gRPC → sealed capsule → verify
============================================================

[step 1] tools/list (read-only) — capsule must NOT be sealed
  ledger unchanged (0 capsules). ✓

[step 2] tools/call submit_order (consequential) → planned + confirmed capsules
  planned:   bff6e52286e87449c6bb…  effect.status=planned
  confirmed: ad985620d916e21d7c8e…  effect.status=confirmed  confirms=bff6e52286e87449c6bb…

[step 3] tools/call get_price (second call) → second planned + confirmed pair

[step 3b] tools/call delete_ledger refused upstream → planned capsule only
  planned:   c7b57b2639e16eeb62b6…  (no outcome — the refusal is visible)

[step 4] Ledger: 5 capsule(s) sealed
  bff6e52286e87449… submit_order [executed] effect=planned runtime=agentgateway
  ad985620d916e21d… submit_order [executed] effect=confirmed runtime=agentgateway
  494a6a9fc9b21eea… get_price [executed] effect=planned runtime=agentgateway
  7f3c0dbc4555999b… get_price [executed] effect=confirmed runtime=agentgateway
  c7b57b2639e16eeb… delete_ledger [executed] effect=planned runtime=agentgateway

[step 5] Verify all capsules (offline — no network needed)
  bff6e52286e87449… ok=True  ✓
  ad985620d916e21d… ok=True  ✓
  494a6a9fc9b21eea… ok=True  ✓
  7f3c0dbc4555999b… ok=True  ✓
  c7b57b2639e16eeb… ok=True  ✓
  All capsules verified ok=True.

[step 6] Tamper test: flip one byte in output digest → verify fails
  original digest: …3b1201ab
  tampered digest: …3b1201a0
  verify result:   ok=False  findings: [… 'recomputed … != carried …']
  Tamper detected — ok=False as expected. ✓

Demo complete.
  Verified at: protocol boundary (direct gRPC to ExtMcp service)
  Same call sequence agentgateway uses for every tools/call.
```

The demo drives the ExtMcp gRPC service with the same `CheckRequest`/`CheckResponse` sequence agentgateway uses internally — no agentgateway binary or Rust toolchain required to verify the integration.

## failureMode options

| Mode | Behavior when capsule-emit is unreachable |
|---|---|
| `failOpen` | Tool call continues; no capsule sealed. Use for observability-only deployments. |
| `failClosed` | Tool call is rejected with a policy error. Use when the audit trail is a hard requirement. |

## Integration surface

capsule-emit implements the `agentgateway.dev.ext_mcp.ExtMcp` gRPC service, defined in agentgateway's `ext_mcp.proto`. The Python stubs (`capsule_emit/adapters/ext_mcp_pb2.py`) are committed to the repo and require only `grpcio>=1.60` at runtime.

## Running it against a real gateway (executed 2026-09-14)

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
