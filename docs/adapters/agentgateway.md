# agentgateway extension

The hardened `CapsuleEmitServicer` you already know is the foundation of this extension. agentgateway (the 4th AAIF project) is a high-performance Rust proxy for MCP, A2A, LLM, REST, and gRPC traffic. Its native `mcpGuardrails` hook lets an external Python gRPC service inspect or audit every MCP call before it reaches the upstream server — this is where capsule-emit plugs in.

## How it works

```
LLM agent
  ↓  MCP tools/call
agentgateway (Rust proxy, port 3000)
  ↓  mcpGuardrails gRPC CheckRequest  → capsule-emit ExtMcp service (port 50051)
  ↓  forwards to upstream MCP server
  ↑  response from upstream
  ↑  mcpGuardrails gRPC CheckResponse → capsule-emit (capsule sealed)
  ↑  response to LLM agent
```

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

Every `tools/call` routed through agentgateway now seals an Agent Action Capsule. `tools/list`, `resources/read`, and all other non-call methods produce no capsule.

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

## Correlation design note

agentgateway's `CheckRequest` and `CheckResponse` are separate gRPC calls with no shared call ID in the proto. capsule-emit correlates them with a FIFO deque — correct for MCP stdio transport (one in-flight tool call per session). For concurrent HTTP sessions with overlapping tool calls, inject a call-ID header via agentgateway's metadata CEL config and replace the deque with a keyed dict in `CapsuleEmitServicer`.

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

[step 2] tools/call submit_order (consequential) → capsule sealed
  capsule_id: e6b136ca4dd5e9ca738e…

[step 3] tools/call get_price (second call) → second capsule

[step 4] Ledger: 2 capsule(s) sealed
  e6b136ca4dd5e9ca… submit_order [executed] runtime=agentgateway
  87f2156eaeba3c47… get_price [executed] runtime=agentgateway

[step 5] Verify all capsules (offline — no network needed)
  e6b136ca4dd5e9ca… ok=True  ✓
  87f2156eaeba3c47… ok=True  ✓
  All capsules verified ok=True.

[step 6] Tamper test: flip one byte in output digest → verify fails
  original digest: …3b1201ab
  tampered digest: …3b1201a0
  verify result:   ok=False  findings: ['recomputed … != carried …']
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
