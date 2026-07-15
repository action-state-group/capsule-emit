# Producer-context hygiene: the three-class floor

Every adapter threads some runtime context into the capsules it seals — an
agent name, a call id, sometimes a whole context object handed over by the
framework. This page is the floor all adapters converge on. It exists because
a capsule is content-addressed, tamper-evident, and possibly anchored:
**redaction is unfixable by design — the more durable the record, the higher
the bar for admission.** The only reliable moment to protect a value is
before it enters the record.

The normative text is the profile's Privacy Considerations section
(`agent-action-capsule` → `spec/draft-mih-scitt-agent-action-capsule`).
**On any conflict, the profile text wins** — this page is the adapter
author's working version.

## The three classes

| Class | What | Rule | Why |
|---|---|---|---|
| **Clear-safe** | Opaque correlation handles: agent name, call id, invocation/run id | MAY appear in clear | They join related capsules; they identify workflow, not people. Should be opaque, and must never be derived from end-user identity — an email-derived invocation id is an identifier in costume. Scope matters too: a handle that recurs across one person's many capsules becomes identity by linkage, so scope handles to an invocation or workflow — mint a fresh run id per invocation instead of propagating the session handle. |
| **Digest-only** | Action payloads: tool inputs and outputs | Never in clear; committed by digest (the base emitter does this) | Provable later without being disclosed — selective disclosure is the reveal mechanism. Low-entropy fields inherit the dictionary-attack caveat and the profile's salting guidance. |
| **Never-enters** | End-user identity — anything that resolves to a person or account (user ids, person-bound session ids, account refs) — and secrets (credentials, tokens, keys) | Never enters, **in any form — including as digests, salted or otherwise** | The exclusion is categorical: a bare identity digest re-identifies by dictionary attack, and a *salted* one is identity-derived material baked into an unerasable record — re-identifiable by whoever holds the salt, forever (harvest now, re-identify later). Identity also carries obligations (erasure, retention, purpose limits) an append-only record can't honor — the only compliant admission is absence. Identity *inside a tool payload* is the digest-only class's job (committed within the payload digest, revealed only via selective disclosure); this class is about identity as record fields. The capsule proves conduct, not who the human was. |

The class test is **binding, not naming**: a runtime's random run/thread
handle that resolves only to a workflow is clear-safe plumbing; a "session
id" that resolves to a person's account is identity, whatever it's called.
("Resolves" means from the record alone — with the operator's own lookup
tables, everything resolves eventually; that mapping staying operator-side
is the point.)

## Ship the allow-list as code

Construct context by **allow-list** (name what may enter), never block-list
(name what may not). Block-lists fail open: when the runtime adds a new
context field, a block-list admits it silently; an allow-list excludes it
until someone deliberately admits it.

The ADK adapter is the reference shape — a tuple of permitted
attributes, everything else structurally unreachable (illustrative at
time of writing; the adapter source is the reference):

```python
# Attributes safe to record from a ToolContext. Deliberately excludes
# `session` and anything that can carry end-user identifiers into a
# tamper-evident record.
_SAFE_CONTEXT_ATTRS = ("agent_name", "function_call_id", "invocation_id")
```

The MCP adapter holds the same floor with one documented, deliberate reach:
it reads the session only to record the **client software's** name and
version (`mcp_client_name` / `mcp_client_version`) — software identity, not
user identity — and host provenance is opt-in with an explicit privacy note.
The session is never serialized wholesale. The same floor applies to every
adapter in the tree, current and future (today: LangChain, CrewAI, Hermes,
agentgateway, MCP); the checklist below is the audit.

## Authoring checklist

Before an adapter ships, answer for every context field the runtime offers:

1. Which class is it in? If you can't say, it doesn't enter.
2. Is every clear-safe handle actually opaque — or derived from something a
   person owns?
3. Would the same handle recur across many capsules for the same human?
   Then it's identity by linkage — scope it to the invocation or workflow.
4. Does anything reach `emit_capsule()` outside the allow-list path?
5. If the runtime grew a new context field tomorrow, would your adapter
   admit it without a human deciding? If yes, you have a block-list.
