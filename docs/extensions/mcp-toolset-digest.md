# `ext.mcp` — tool-manifest digest

**Namespaced payload extension.** Lives in `model_attestation.compute_attestation["ext.mcp"]`.
Not a spec field — `effect.request_digest`/`response_digest` keep their -02 I/O semantics
(actual protected-action request / actual observed response), untouched by this extension.

## Why

A capsule proves what the agent **did** — it does not prove which tool descriptions the
model was **shown** at decision time. NSA CSI *"MCP: Security Design Considerations"*
(May 2026, U/OO/6030316-26) documents a real attack shape: an MCP server behaves benignly
at install/first use, then swaps a tool's description (or capability) once it has gained
trust — often without any re-approval step. Without this extension, that swap is invisible
in the record: the capsule chain looks identical before and after.

`ext.mcp` closes that gap by committing the tool manifest itself to a digest, carried on
every capsule. A swap becomes a visible digest change between adjacent capsules in the
chain — inspectable in the raw record today; a dedicated "toolset changed mid-session"
chain annotation on the verify surface is a follow-up, not implemented here (see
[Verify surface](#verify-surface) below).

## Field shape

```json
{
  "ext.mcp": {
    "toolset_digest": "be4e0ddb7c141118eb5ed9df1f1a5dce40d32c984eedf4a9c76797fb9383a9b0",
    "digest_alg": "SHA-256",
    "manifest_ref": {
      "type": "MCPToolManifest",
      "digest_alg": "SHA-256",
      "digest": "be4e0ddb7c141118eb5ed9df1f1a5dce40d32c984eedf4a9c76797fb9383a9b0"
    }
  }
}
```

- `toolset_digest` — the fingerprint. Present on every capsule once
  `MCPCapsuleEmitter.capture_toolset(tools)` has been called. Stays byte-identical across
  capsules while the toolset is unchanged; a description/schema/name change anywhere in the
  manifest changes it.
- `digest_alg` — always `"SHA-256"` today. Present at both the `ext.mcp` level and inside
  `manifest_ref` so a verifier reading either field independently still knows the algorithm.
- `manifest_ref` — a typed reference to the manifest bytes: `{type, digest_alg, digest}`,
  `digest` identical to `toolset_digest`. Its presence does not mean the bytes are attached
  to *this* capsule — see [Manifest artifact](#manifest-artifact).

## Digest context — exact projection + canonicalization

This is the part a verifier must reproduce exactly to confirm a `toolset_digest`. Given the
tool manifest as presented to the model (an MCP `tools/list` response, or the equivalent
list of tool descriptors for any other agent-tool framework):

1. **Project** each manifest entry to exactly three fields:
   - `name` — the tool name (required; a manifest entry without a name is an error).
   - `description` — the tool description shown to the model. Missing/`None` → `""`.
   - `input_schema` — the tool's input JSON Schema (MCP's `inputSchema`, camelCase on the
     wire; normalized to the snake_case key `input_schema` in the projected form — the wire
     casing is not part of the digest context). Missing/`None` → `{}`.

   Nothing else enters the digest: no `title`, `outputSchema`, `annotations`, `icons`, or
   `meta` fields, even when the underlying SDK object carries them. A change to any of
   those fields — real in some MCP SDKs — does **not** move `toolset_digest`. This is a
   deliberate scope boundary (match the CSI's threat: description/capability swap), not an
   oversight; widening the projection is a compatible future revision if a swap vector shows
   up in a field outside it.

2. **Sort** the projected list by `name` (ordinary string sort). The digest is therefore
   **order-independent** — a server that returns the same tools in a different order every
   call does not produce spurious digest churn. Only a content change (name, description, or
   schema) moves the digest.

3. **Canonicalize + digest**: `JSON-DIGEST` — SHA-256 of the RFC 8785 (JCS) serialization of
   the sorted projected list, after the spec's absent-field normalization (§2 of
   `draft-mih-scitt-agent-action-capsule`). This is the exact same canonicalization
   `capsule_id` and every I/O digest in this library already use
   (`agent_action_capsule.canonical.json_digest`) — no new canonicalization scheme was
   introduced for this extension.

Reference implementation: `_project_tool`, `_project_toolset` in
[`capsule_emit/adapters/mcp.py`](../../capsule_emit/adapters/mcp.py).

**Known limitation:** a JSON Schema numeric field expressed as a float (e.g.
`"minimum": 0.5`) fails the digest with `FloatInDigestError` — the same fail-closed rule the
whole library applies to any digest-bearing value (§5.1: no raw JSON floats). Integer bounds
digest fine; this only bites a schema that genuinely needs a fractional bound.

## Manifest artifact

Preferred practice, `emit_manifest_artifact=True` (the constructor default): the **first**
`capture_toolset()` call in a session, and every subsequent call whose digest differs from
the current one, write the exact canonical bytes —
`jcs(normalize(projected_list))`, the literal JCS preimage — to
`<ledger-dir>/<ledger-stem>.mcp-manifests/<digest>.json` (override the directory with
`manifest_artifact_dir=`). A call with an unchanged digest writes nothing (no churn while the
toolset is stable).

This makes `toolset_digest` **openable evidence**, not just an unfalsifiable fingerprint: any
party holding the artifact file recomputes `SHA-256(file bytes)` directly — no
re-serialization step, no whitespace/key-order ambiguity — and compares it to
`toolset_digest`/`manifest_ref.digest` on any capsule in that run.

`emit_manifest_artifact=False` withholds the bytes while keeping `toolset_digest` and
`manifest_ref` visible on every capsule — the standard selective-disclosure posture
("digest visible, bytes withheld"). A verifier without the bytes still gets the fingerprint
and can still detect a swap (digest inequality between capsules); it just cannot confirm
*what* the manifest said without the bytes from some other channel.

## Usage

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

emitter = MCPCapsuleEmitter(operator="acme-co", developer="po-agent@v1")

# FastMCP: the real tools/list response — literally what the model is shown.
tools = await app.list_tools()
emitter.capture_toolset(tools)          # once at startup, and again on any change

@emitter.tool("write_order")
def write_order(vendor: str, total: float) -> dict:
    ...
```

Plain dicts work too (no MCP SDK required, matching the rest of this adapter):

```python
emitter.capture_toolset([
    {"name": "write_order", "description": "Submit a purchase order.", "inputSchema": {...}},
])
```

## Verify surface

No renderer changes ship with this extension. `ext.mcp` renders opaque on the verify
surface today, the same as any other unrecognized `compute_attestation` key — the raw
record view already shows the field and its value changing between capsules, which is
enough to spot a swap by inspection. A dedicated "toolset changed mid-session" chain
annotation (flagging the exact boundary capsule automatically) is a candidate follow-up,
not built here.
