# `org.agentactioncapsule.otel` — OpenTelemetry correlation (v0, digests-only)

**Namespaced payload extension**, per draft-palanisamy-scitt-aac-otel-00 ("OpenTelemetry
Correlation Extension for Agent Action Capsules", under review). Lives in
`model_attestation.compute_attestation["org.agentactioncapsule.otel"]` — see
[Where the block lives in v0](#where-the-block-lives-in-v0) for why, not the draft's own
top-level-payload-member framing.

## Why

Agent runtimes already emit OpenTelemetry spans for tool calls, model invocations, and
handoffs. A Capsule seals a signed, content-addressed record of the same actions at the
effect boundary. Without this extension the two are joinable only by convention — matching
timestamps, hoping nothing raced. This extension makes the join explicit and cheap to
verify: a Capsule carries digests of the trace/span identifiers that observed the action,
and (best-effort — see below) the span carries the Capsule's own id.

## What ships in v0

`capsule_emit.otel.CapsuleOTelSpanExporter` — an OpenTelemetry Python SDK `SpanExporter`
(`pip install "capsule-emit[otel]"`):

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from capsule_emit.otel import CapsuleOTelSpanExporter

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(
    CapsuleOTelSpanExporter(operator="acme-co", developer="my-agent@v1")
))
```

Every span it sees is classified `observation`/`effect` via the two-signal taxonomy
(`docs/whats-consequential.md`, "Reading Signal 1 off an OpenTelemetry span" —
`capsule_emit.otel.classify_span_signal_1`). Only `effect` spans are sealed — the same
"why reads are not sealed by default" posture every other capsule-emit adapter takes.

**Python reference, not a Go collector-distribution build.** capsule-emit has no Go code;
this composes at the OpenTelemetry Python SDK level, the same way every other adapter here
plugs into its host framework's own seam. A standalone `otelcol` processor is a separate
build (in `capsule-cli` or a new repo), out of scope here.

## The block

Every field below is **digest-only by default** — v0's own conservative choice on top of the
draft (see [Why v0 defaults every ID to a digest](#why-v0-defaults-every-id-to-a-digest)):

| Field | v0 default | Opt-in clear (`clear_trace_context=True`) |
|---|---|---|
| `trace_id` | `SHA-256(trace_id)` | raw 32-hex |
| `span_id` | `SHA-256(span_id)` | raw 16-hex |
| `parent_span_id` | `SHA-256(parent_span_id)` | raw 16-hex |
| `span_name` | `SHA-256(span_name)` | raw string |
| `trace_flags` | raw 2-hex (no privacy concern named in the draft) | — |
| `tracestate_digest` | always `SHA-256(tracestate)` | never clear — the draft is unconditional here |
| `resource.*` | only `service.name`/`service.version`/`service.namespace`/`deployment.environment.name`/`telemetry.sdk.{name,version}`, clear | — |
| `semconv.*` | draft's own per-attribute tier (`capsule_emit.otel.allowlist.SEMCONV_ATTRS`) | — |

**Allow-list, default-deny.** Any attribute not in `SEMCONV_ATTRS` — including everything the
draft marks `never-enters` (prompt/completion content, tool arguments/results, memory/retrieval
text, session/end-user identifiers) — is silently dropped. It is never read into a digest,
never touched, full stop. `tests/test_otel_processor.py`'s
`test_LEAK_MUTANT_never_enters_key_promoted_to_clear_safe_leaks_through_real_builder` proves
this by running the REAL block builder against a deliberately-poisoned allow-list and showing
the leak, then the same call against the real table showing none.

## Where the block lives in v0

The draft frames `org.agentactioncapsule.otel` as a **top-level payload member**, parallel to
`action_id`/`operator`/etc. Nothing in the currently-released `agent_action_capsule.emit()`
(spec -04, format_version `"4"`) accepts an arbitrary top-level member yet — only
`compute_attestation` (nested under `model_attestation`) is extensible today, the same
container [`ext.mcp`](mcp-toolset-digest.md) already uses. v0 places this block there too. The
draft's own editor note anticipates this ("AAC -05 is expected to state the two extension
containers explicitly ... Nothing here changes if it does") — only the JSON path moves if/when
that lands; the field names, tiers, and shape do not.

## Why v0 defaults every ID to a digest

The draft leaves `trace_id`/`span_id`/`parent_span_id`/`span_name` clear-safe *conditional* on
a producer privacy assessment this library cannot perform mechanically: "Producers MUST
classify trace and span IDs as clear-safe only when the observability systems they index do
not themselves hold end-user identity ... Otherwise the IDs MUST be carried as digests." v0
takes the safe branch as the default and makes the clear-text branch an explicit,
per-deployment opt-in (`clear_trace_context=True`) — never a default a deployment falls into
without deciding.

## Outcome-context tagging — shipped empty, by design

`org.agentactioncapsule.otel` can never carry OpenTelemetry Baggage entries — the draft is
unconditional: "no field of `org.agentactioncapsule.otel` MAY carry: ... OpenTelemetry baggage
entries ... clear or as a digest." Outcome-context tagging (tagging a sealed span with a
baggage-carried exchange/reconciliation state) is therefore a **sibling** compute_attestation
key, `ext.otel.outcome_context`, never nested inside the block above.

`outcome_context_baggage_keys` is an explicit, caller-supplied allow-list — **empty by
default**. No design note naming the real baggage keys exists in the workspace as of v0 (the
task that built this searched `_work/`/`_ops/`/both lane buffers; the only "Area 16" hit found
was an unrelated reconciliation-states item) — see the outbox `Needs decision` entry filed
alongside this. Configuring the real keys once they exist is a one-line change:

```python
CapsuleOTelSpanExporter(
    operator="acme-co", developer="my-agent@v1",
    outcome_context_baggage_keys=frozenset({"the.real.key.once.it.exists"}),
)
```

Values are always digested — baggage is unstructured, caller-supplied string data with no
allow-list of its own, unlike `semconv`.

## The reverse join: `aac.capsule_id` on the span

Provisional attribute name per the draft — expect a rename to `gen_ai.evidence.*` once the
OpenTelemetry semantic-conventions registry assigns one; `capsule_emit.otel.REVERSE_JOIN_ATTRIBUTE`
is the one place that rename lands.

**This is best-effort in `CapsuleOTelSpanExporter`, and that is an OpenTelemetry SDK
constraint, not a bug here.** `SpanExporter.export()` receives already-ended, immutable
`ReadableSpan` objects — the OTel Python SDK's own `Span.set_attribute()` silently no-ops once
`end()` has been called, on ANY span, including the one just exported. There is no supported
way for an exporter to amend the very span it is exporting. v0 calls
`opentelemetry.trace.get_current_span().set_attribute(...)` from inside `export()`, which
reaches a *different*, still-open span (typically the parent, when export fires synchronously
inside the child's own `end()`, e.g. under `SimpleSpanProcessor`) and is a no-op when nothing
is open (async/batched export on a background thread).

For a **guaranteed-correct** join on the same span, call `capsule_emit.otel.stamp_reverse_join`
directly from application code, before that span's own `span.end()`:

```python
from capsule_emit.otel import stamp_reverse_join

with tracer.start_as_current_span("write_order") as span:
    result = seal(order_payload, operator="acme-co", developer="my-agent@v1")
    stamp_reverse_join(span, result.capsule_id)  # before span.end() (the `with` block's exit)
```

The span attribute is advisory either way — the draft is explicit that a verifier MUST
recompute `capsule_id` from the Capsule itself; a disagreeing span attribute is a defect in the
span, not in the Capsule.

## Verification

This extension defines no verification behavior beyond the base profile's. The block's
presence, absence, or content never changes a Capsule's Class 1/2 verdict — a verifier that
does not implement this extension ignores it entirely.
