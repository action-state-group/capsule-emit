# Design note: how capsules ride OpenTelemetry spans and logs

**Status:** design only. Nothing here ships, and no record key, attribute or
API changes with this note. Tracking issue:
[#482](https://github.com/action-state-group/capsule-emit/issues/482).

**Scope.** A pilot adopter running agentgateway as its only egress asked
whether the model's tool-call decisions should be recorded as well as the MCP
calls that go out. The product answer for now is no: phase 1 is MCP effect
records plus the completeness controls in
[#471](https://github.com/action-state-group/capsule-emit/issues/471), and
decision records are deferred and designed later with the adopter. The adopter
is expected to run OpenTelemetry, so two trace-based options lead:

- **A. Seal the trace export.**
- **B. Match a decision to its effect on trace id.**

This note records what capsule-emit's OpenTelemetry code does today, what each
option would take and prove, what an adapter would have to carry, and what
stays out of every record. The decisions it leaves for the product owner are
listed at the end.

agentgateway facts below are read from its source at commit `134cbf02`
(2026-11-24), not from a live gateway.

## 1. The rule for spans and logs: a reference, never a copy

The direction from telemetry to capsule is already set by the AAC
[telemetry-binding profile](https://github.com/action-state-group/agent-action-capsule/blob/main/docs/telemetry-binding-profile.md):
a telemetry event MUST carry a reference to a capsule, never a copy of one,
and it is never evidence on its own. This note adds nothing to that rule. A
span or an OpenTelemetry log record carries at most the capsule id; the
capsule carries at most digests of the trace and span ids. Each side can find
the other, and neither repeats the other's content.

| Direction | What is carried | Where it ships today |
|---|---|---|
| capsule → trace | `trace_id`, `span_id`, `parent_span_id` as SHA-256 digests by default | `org.agentactioncapsule.otel` block, `python/capsule_emit/otel/block.py:147-226` |
| span → capsule | the capsule id as span attribute `aac.capsule_id` | `stamp_reverse_join`, `python/capsule_emit/otel/processor.py:223-230` |
| log record → capsule | nothing ships | an application may put the capsule id on a log record that already carries the trace context |

One gap to close when this path is built: a span stamped with
`aac.capsule_id` is not a profile event. capsule-emit's attribute name is a
placeholder (`processor.py:76-79`); the profile names the value
`receipt.digest` and also makes `action.verb` and `decision` (its guard
outcome, `ALLOW`, `DENY` and so on) required fields, with conformance rules of
its own. A rename alone would not make the span conform. Note too that the
profile's `decision` is a guard's verdict, not the model's tool choice this
note calls a decision.

## 2. What capsule-emit's OpenTelemetry exporter seals today

`CapsuleOTelSpanExporter` is an OpenTelemetry Python SDK `SpanExporter`, not a
Collector component (`processor.py:4-12`). It plugs into a Python
application's `TracerProvider`; it cannot run inside an OpenTelemetry
Collector or inside agentgateway.

For each ended span it:

1. **Classifies** the span by its own attributes
   (`signal.py:100-117`). Only `EFFECT` spans are sealed
   (`processor.py:178-179`). A span with no deciding attribute is `EFFECT` by
   the fail-safe default (`connector.py:203`). That includes a GenAI chat span
   with only `gen_ai.*` attributes: checked on `main` at `f537bab`,
   `classify_span_signal_1("chat gpt-4o", {"gen_ai.operation.name": "chat"})`
   returns `EFFECT`.
2. **Builds the block**, default-deny (`block.py:147-226`): trace, span and
   parent ids as `SHA-256(id)` unless the deployment sets
   `clear_trace_context=True` (`block.py:197-202`); `tracestate` only as a
   digest, with no opt-out (`block.py:207-211`); the span name only with that
   opt-in (`block.py:213-214`); six resource attributes
   (`allowlist.py:81-88`); and the GenAI attributes in `SEMCONV_ATTRS`
   (`allowlist.py:95-138`), with provider response ids digested.
3. **Seals** one record with `action_type="fyi"` and an effect status of
   `confirmed` or `failed` from the span status (`processor.py:205-217`). The
   span name reaches `action_id` and `effect.type` only as a digest unless
   the opt-in is set (`processor.py:180`).

It never sets `aac.capsule_id` on the exported span, because an ended span
cannot be changed (`processor.py:23-40`); the application does that with
`stamp_reverse_join` while the span is still open.

`seal_otlp_file` (`python/capsule_emit/otel/backfill.py`) runs the same path
over an exported OTLP/JSON file, one document or JSON Lines
(`backfill.py:26-27`). Each record is `backfilled` with
`time_rung: "self_attested"` (`backfill.py:128-134`), carries a `source_ref`
digest of the trace and span ids, the file's digest as `import_batch`, and
the span's own start and end under `ext.otel.span_time`. A re-run skips spans
whose `source_ref` the ledger already holds.

The agentgateway MCP adapter seals the effect records phase 1 relies on, and
carries no trace context: it does not read request headers
(`python/capsule_emit/adapters/agentgateway.py:317-318`), so nothing in a
`planned` or outcome record names the trace the call belongs to.

## 3. Option A: seal the trace export

The adopter's OpenTelemetry pipeline already writes the traces somewhere. We
seal what it wrote, inside their network.

**A1. One record per export file.** Seal one record whose payload names the
export and its SHA-256, for example with `seal()` (signed) or `log()`
(unsigned). Nothing new is needed. This proves the file has not changed since
it was sealed, and nothing about what is in it. It covers every span,
including the ones the classifier would skip.

**A2. One record per effect span**, with `seal_otlp_file` as it ships. Each
record is `backfilled`, so it never claims capture time. A Collector's file
exporter writing OTLP/JSON Lines is the natural input; sealing it on a
schedule works because re-runs skip spans already sealed. A2 records each
file's digest as `import_batch`, so it also gives A1's file check.

What neither A1 nor A2 gives:

- **No link from a decision to its effect.** They prove the trace was not
  edited after sealing, not which tool call an LLM response led to.
- **No "decided X" on the LLM span.** On a chat span the tool the model chose
  sits only in the response content (`gen_ai.output.messages`), which never
  enters a record (`allowlist.py:146-162`). An LLM span sealed under A2 shows
  that a model call happened in that trace and, through
  `gen_ai.response.finish_reasons`, that it ended asking for tools, but not
  which tool. The tool that *ran* is a different matter: agentgateway puts
  `gen_ai.tool.name` on its own MCP upstream span
  (`crates/agentgateway/src/mcp/upstream/mod.rs:163-165`), that attribute is
  clear-safe (`allowlist.py:104`), and such a span classifies `EFFECT`, so A2
  over an export holding gateway spans already seals which tool ran in the
  trace. The record's `action_id` and `effect.type` stay digests of the span
  name by default (`processor.py:180`).
- **No proof of time.** A backfilled record proves the trace existed when it
  was sealed, not when the span says it ran.
- **A misleading kind.** Under A2 an LLM span is sealed as an effect record
  by the fail-safe default. A decision is not an effect; if A2 is offered for
  decisions, the record must say what it is (decision 3 below).
- **Not an effect count.** The same default seals other gateway spans that
  carry no read or write signal: `tools/list` and the MCP session `GET` and
  `DELETE` spans carry only `mcp.method.name` and the target
  (`crates/agentgateway/src/mcp/upstream/mod.rs:158-165`, `:274-276`), and
  the guardrail gRPC spans carry the old key `http.method`
  (`httpproxy.rs:5403`), which the classifier does not read. Each becomes an
  effect record, so one tool call yields several records, reads included,
  while phase 1 filters reads out at the gateway (`agentgateway.py:6-7`). A2
  records reconcile with phase 1 records only after the export is filtered,
  for example to `mcp.method.name == "tools/call"`.

A is the smallest step and needs no build for A1 and none for A2 beyond
running it. It protects the adopter's own trace store; it does not answer
"decided X, did X".

## 4. Option B: match a decision to its effect on trace id

If the agent sends one W3C trace context on both its LLM call and its MCP
calls, both go through agentgateway in the same trace. The MCP effect record
then names that trace by digest, and a reviewer holding the adopter's trace
export can find, for each effect, the LLM calls in the same trace.

### What an adapter must carry

1. **The agent propagates trace context.** The agent's LLM request and its
   MCP `tools/call` request carry `traceparent` from the same trace. This is
   the adopter's side, and it is the question to ask them first. agentgateway
   keeps the client's trace when it forwards a request: its test
   `tracing_parent_not_sampled_exports_and_propagates_sampled_flag`
   (`crates/agentgateway/tests/tests/basic.rs:376-379`) asserts the upstream
   `traceparent` "must stay in the client's trace". That test drives a plain
   HTTP route; the MCP and LLM routes are expected to behave the same and must
   be checked on the adopter's version.
2. **The adapter reads the trace context agentgateway already sends.** Two
   sources exist; the first leads:
   - **gRPC metadata (lead).** When gateway tracing is on, agentgateway starts
     a span for each `CheckRequest` and `CheckResponse` call it makes to our
     processor and injects its trace context into that gRPC call
     (`crates/agentgateway/src/mcp/guardrails/client.rs:64-68`, `:246-250`;
     `crates/agentgateway/src/proxy/httpproxy.rs:5399-5411`). It is present on
     both phases, needs no header forwarding, and names a gateway span that
     appears in the adopter's own export. It is absent when gateway tracing is
     off (`httpproxy.rs:5399-5401`) or the request is not sampled: the
     gateway makes no span then (`crates/agentgateway/src/telemetry/log.rs:1218-1224`)
     and passes the client's `traceparent` through untouched
     (`basic.rs:474-480`), so under head sampling only the sampled share of
     effects gets it. Unsampled traces are missing from the export too, so
     those effects cannot be matched under A either. It carries `traceparent`
     only, never `tracestate` (`log.rs:2929-2935`).
   - **`McpRequest.headers` (fallback).** ExtMcp also delivers the incoming
     HTTP headers, after the gateway's allow/deny filter, on the request phase
     only; `McpResponse` has none, and stdio upstreams have none
     (`crates/protos/proto/ext_mcp.proto:28-31`, `:34-43`). Using it needs
     `requestHeaders: {allowed: [traceparent, tracestate]}`, which also keeps
     `authorization` off the wire; an `allowed` list without `traceparent`
     yields nothing. Its span id is the agent's own client span. It is the
     only source of `tracestate`, and it still carries the agent's
     `traceparent` on unsampled requests.
3. **The adapter seals it as the existing block.** It parses `traceparent`
   and adds an `org.agentactioncapsule.otel` block through `build_otel_block`
   (`block.py:147-226`), so the trace id stays a digest by the same default as
   the exporter. The block requires a `span_id`; it holds the span the
   `traceparent` names (the gateway's guardrail span from gRPC metadata, or
   the agent's client span from the fallback), never an id presented as a
   span of capsule-emit's own. The exact field mapping is decided, and
   reviewed as a format change, in the item that builds it. With the
   fallback, the outcome record copies the trace from its planned entry; an
   outcome sealed unchained because pairing was ambiguous
   (`agentgateway.py:50-56`) then has none, which is a reason the gRPC source
   leads.
4. **The reviewer digests the export's ids.** The join key is the block's
   `trace_id`: `SHA-256` over the 32-character lowercase hex string as UTF-8
   bytes (`block.py:80-88`, `:197`). It is not the backfill `source_ref`,
   which hashes the raw id bytes of trace and span together
   (`backfill.py:158-159`).

When a missing or malformed `traceparent` leaves no block, the record says
nothing about a trace, and its effect shows as "no trace" (below). The item
that builds this also changes the adapter's stated contract: the module says
today that the adapter never reads headers (`agentgateway.py:78-82`,
`:317-318`), and if the header fallback is used, the reference configs in
`docs/adapters/agentgateway.md`, which forward every header but
`authorization` (`agentgateway.md:116-118`, `:245`, `:396`), change to the
narrower allow list.

A trace id is 128 bits and normally random, so its digest cannot be guessed
the way a digest of an account number can. That is why B avoids the
guessable-digest problem a join on argument digests has, without a keyed
digest.

### What the match proves, and what it does not

- **Same trace, not same call.** One agent turn can hold several LLM calls
  and several tool calls in one trace. B groups them; it does not pair one
  decision with one effect. Pairing one to one needs a per-call id on both
  sides, which is not being built.
- **Shown per effect in one of four states**: *matched* (one LLM call in the
  trace before it), *unmatched* (the trace is known and holds no LLM call
  before it, which is the real signal: an effect with no decision ahead of
  it), *unresolved* (several LLM calls before it), or *no trace* (the record
  carries no trace context, which says only that the context did not
  arrive). This follows the recommendation of best effort; whether the product presents it that way is not ruled yet
  (decision 2).
- **The trace id is the agent's claim.** The gateway continues whatever trace
  the agent started, so a faulty or hostile agent can put any trace id on its
  calls; the gRPC source makes the span id a real gateway span, not the trace
  id trustworthy. B is correlation for review, never proof that a decision
  caused an effect.
- **"No trace" is not an error**: the agent did not propagate trace
  context, gateway tracing was off or the request was not sampled, or (with
  the header fallback) the call came over stdio.

B is a small build in this repository (items 2 to 4) plus a configuration
change on the adopter's side (item 1). It adds a block to agentgateway
records, which is a format change and gets its own review when it is built.

### A and B together

B on the effect records plus A2 on the adopter's trace export gives both
sides sealed: the effect records live, and the LLM spans backfilled, joined
by trace id digest. This is the shape to offer when the adopter's business
units ask for "decided X, did X".

## 5. What stays out

- **No clear prompt, completion or tool-argument text** in any record or span
  attribute. For today's records this is the shipped default; for decision
  records it is the standing recommendation, not a ruling (decision 5). The never-enters rows (`allowlist.py:146-170`) cover messages,
  system instructions, tool arguments and results, memory and retrieval text,
  conversation and MCP session ids, and `enduser.*`/`user.*`. The allow-list
  is default-deny, so an attribute not listed never leaves the process
  (`block.py:123-144`). A customer who needs clear text keeps its own copy in
  its own domain.
- **No capsule body on a span or log record**, only the id (section 1).
- **No `tracestate` in clear**; vendor entries can carry identifiers.
- **No other request header.** B reads `traceparent` (and, with the header
  fallback, `tracestate`) only; the adapter keeps ignoring every other header.
- **Tool arguments on the MCP record stay as they are today.** B adds no
  argument handling. Their digests are unsalted today, so a low-entropy
  argument is guessable from its digest; that is a separate known limit, not
  part of this design.

## 6. Decisions for the product owner

None is needed for phase 1. When the decision-record path is opened with the
adopter:

1. **Which option to offer first.** A alone (seal the adopter's trace export,
   no link between decision and effect), B alone (trace id on the effect
   records, matched against the adopter's unsealed traces), or A and B
   together. Recommendation: A and B together, since B without A matches
   against a trace store nobody sealed.
2. **How the match is presented.** As a best-effort, trace-level match shown
   in the four states above, or not at all until a per-call id
   exists. Recommendation: best effort, never called a guarantee. Not
   ruled yet.
3. **What an LLM span sealed from a trace is called.** Today it would be an
   effect record by the fail-safe default. Options: keep it and say so in the
   report, or mark it as a decision (a record kind or field the AAC
   specification would have to define). Recommendation: decide before A2 is
   offered for decisions, since an adopter would read an effect record as
   "this happened".
4. **Whether phase 1 effect records start carrying the trace id now.** A
   record sealed without it can never be matched later. Adding it is the B
   build above, ahead of the rest. Recommendation: only once the adopter
   confirms its agents propagate trace context and that gateway tracing
   samples the calls it wants matched, since otherwise the field is always
   empty.
5. **Whether decision records stay digest-only.** Recommendation: yes; a
   customer who needs clear text keeps its own copy and discloses it from its
   own domain. Not ruled yet.
