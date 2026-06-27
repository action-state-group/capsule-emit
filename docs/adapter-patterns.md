# Adapter patterns: gateway vs. decorators

capsule-emit offers two ways to seal capsules. They look similar but operate at
different points in the call graph, evaluate different signals, and serve
different populations. Choosing the wrong one produces a gap or needless noise.

See [whats-consequential.md](whats-consequential.md) for the two-signal rule
that governs *when* a capsule should be produced. This page covers *where* to
produce it.

> **Design reference:** §6 (mixed fleets / exclusion policy), §9 (effect
> classifier), §11 (two-signal rule) of
> `action-state-strategy/docs/design/design-where-capsules-are-produced.md`.

---

## Pattern A — gateway (agentgateway `mcpGuardrails`)

```
LLM agent
  ↓ MCP tools/call
agentgateway  ──→  CheckRequest  →  capsule-emit (input captured)
  ↓ upstream MCP server
  ↑ response
agentgateway  ──→  CheckResponse →  capsule-emit (capsule sealed)
  ↑ LLM agent
```

**Who it seals:** every `tools/call` routed through the gateway, regardless of
which MCP server or agent framework is behind it.

**How reads are excluded:** at the gateway config layer — only methods listed in
`methods:` fire the `mcpGuardrails` hook. Everything not listed passes through
un-sealed, without the capsule-emit service ever seeing it.

**What the developer controls:** the `methods:` allow-list in `config.yaml`.

**When to use it:**
- You want fleet-wide coverage without touching every MCP server.
- You cannot modify the agent code (third-party servers, managed agents).
- You want a single policy point for consequential-call sealing across many
  agents with one gateway.

**What it cannot do:**
- Evaluate Signal 2 (privileged reads) — the gateway sees traffic bytes, not
  data classification tags. Signal 2 belongs in the engine.
- Seal effects that never cross the gateway wire (local database writes, engine
  decisions made before the MCP call).

---

## Pattern B — decorators (`@emitter.tool()`)

```python
emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="goose-agent@v1",
    anchor=False,
    seal_reads=False,   # opt-in: skip fyi tools entirely
)

@server.tool()
@emitter.tool()            # wraps only this tool
def submit_order(vendor: str, amount: float) -> dict:
    ...

@server.tool()
@emitter.tool(action_type="fyi")   # explicitly a read; skipped when seal_reads=False
def get_price(item: str) -> float:
    ...
```

**Who it seals:** exactly the tools you wrap — no more, no less.

**How reads are excluded:** developer-explicit. Wrap commands; do not wrap
reads. If you wrap a read (e.g. for Pattern A parity or a sensitive-read
policy), label it `action_type="fyi"` and set `seal_reads=False` on the emitter
to skip sealing entirely.

**What the developer controls:** which functions get the decorator, and whether
`fyi` tools are sealed or skipped.

**When to use it:**
- You own the MCP server code and can annotate individual tools.
- You want per-tool control over what seals and what does not.
- You are building a Goose extension or a bespoke agent and want sealing
  co-located with the tool logic.

**What it cannot do:**
- Cover tools on other MCP servers automatically.
- Evaluate Signal 2 autonomously — the decorator does not know the resource
  classification. For privileged reads, the engine calls `emit()` directly.

---

## The key asymmetry

| Dimension | Gateway | Decorators |
|---|---|---|
| Scope | All agents behind the gateway | Only wrapped tools |
| Signal 1 filter | Config (`methods:` allow-list) | Developer choice (wrap or don't) |
| Signal 2 (sensitive reads) | Not evaluated | Not evaluated (engine-side only) |
| `fyi` tool control | N/A (method never reaches hook) | `seal_reads=False` skips entirely |
| Reads | Never reach capsule-emit | Reach emitter only if you wrap them |
| Unknown methods | Unsealed (allow-list, not deny-all — see below) | Sealed (fail-safe default) |
| Code change required | No (gateway config only) | Yes (add decorator) |

### Gateway allow-list vs. decorator fail-safe

The gateway config `methods: {"tools/call": full}` is a deliberate allow-list:
only the listed methods fire the capsule-emit hook. Any MCP method not in the
list passes through un-sealed.

For **current MCP** this is safe — `tools/call` is the only method that
executes tool logic and mutates external state. All other MCP methods
(`tools/list`, `resources/read`, `prompts/get`, etc.) are read-only.

The allow-list does **not** satisfy the §9 "unknown → gated" principle. A
future MCP method that executes consequential logic would be silently unsealed
unless the operator adds it to `methods:`. **Operators should review their
`methods:` config whenever the MCP spec adds new methods.**

Decorators behave correctly: a tool with no `action_type` (unknown) is sealed
by default — fail-safe.

---

## Mixing both patterns

In a fully-instrumented deployment both patterns run together:

- **Gateway** seals every `tools/call` at Class 1 (infrastructure verdict,
  confirmed-effect digest).
- **Engine-backed tools** sealed a Class 2 capsule upstream (business
  constraints, manifest reasoning); the gateway **chains** a thin Class 1
  capsule by `decision_id` rather than double-sealing.
- **Decorator-wrapped tools** on servers behind the gateway: the engine capsule
  and the gateway capsule are chained — the engine proves *what was decided*,
  the gateway proves *what actually went out*.

See the design reference above (§2 "two vantage points") for the full
Class 1 / Class 2 composition model.

---

## Quick decision guide

```
Is the agent code yours to modify?
  No  → Pattern A (gateway)
  Yes → Do you want fleet-wide coverage or per-tool control?
          Fleet-wide → Pattern A (gateway) or both
          Per-tool   → Pattern B (decorators)

Do you need to seal privileged reads (Signal 2)?
  → Engine-side direct emit() call; see whats-consequential.md §Signal 2
```
