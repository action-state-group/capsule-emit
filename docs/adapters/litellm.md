# LiteLLM

`capsule-emit[litellm]` ships `LiteLLMCapsuleListener` — a `CustomLogger` that
seals a planned → outcome chain around every LLM call a LiteLLM proxy serves.

It is an **out-of-tree** adapter. Nothing is forked, vendored, monkeypatched, or
merged into `litellm`: the proxy's own config-callback loader imports it from the
installed package. One line of `config.yaml` is the whole integration.

```yaml
# config.yaml
litellm_settings:
  callbacks: ["capsule_emit.adapters.litellm_listener.proxy_handler_instance"]
```

```bash
export CAPSULE_EMIT_OPERATOR=acme-co
export CAPSULE_EMIT_DEVELOPER=support-gateway@v1
export CAPSULE_EMIT_LEDGER=/var/lib/capsule/ledger.jsonl
litellm --config config.yaml
```

| Moment | Capsule |
|---|---|
| the request | `effect.status="planned"` — the chain parent |
| the response (`async_log_success_event`) | `effect.status="confirmed"`, `confirms`-chained |
| an upstream failure (`async_post_call_failure_hook`) | `verdict="errored"`, `effect.status="failed"`, chained |

In plain Python (no proxy), append an instance to `litellm.callbacks`:

```python
import litellm
from capsule_emit.adapters.litellm_listener import LiteLLMCapsuleListener

listener = LiteLLMCapsuleListener(operator="acme-co", developer="support-gateway@v1")
litellm.callbacks.append(listener)
```

## How the config line actually loads

`litellm.proxy.types_utils.utils.get_instance_fn` splits the dotted string,
`importlib.import_module`s everything but the last component and `getattr`s the
last one. `initialize_callbacks_on_proxy`
(`litellm/proxy/common_utils/callback_utils.py`) then passes the result through
`_loaded_callback_or_raise`, which **rejects a class**: only a `CustomLogger`
*instance* or a plain callable is dispatchable. Point the config at the class and
the proxy refuses to start, with a message telling you to point it at an instance
instead.

That is why the dotted path ends in `proxy_handler_instance` and not in
`LiteLLMCapsuleListener`. The instance is built lazily, on attribute access
(PEP 562 module `__getattr__`), so importing the module in your own tests has no
side effects while the loader still gets a live object.

**One gotcha worth knowing.** When `get_instance_fn` is called with a
`config_file_path` — which is exactly what config load does — it first checks for
a *file* at `<dirname(config.yaml)>/capsule_emit/adapters/litellm_listener.py`
and only falls back to importing the installed package if that path does not
exist. A stray `capsule_emit/` directory next to your `config.yaml` will shadow
the installed package silently.

### Configuration

The config-callback path passes no constructor arguments, so operator identity
comes from the environment:

| Variable | Meaning |
|---|---|
| `CAPSULE_EMIT_OPERATOR` | **required** — tenant/org stamped on every capsule |
| `CAPSULE_EMIT_DEVELOPER` | **required** — agent name + version |
| `CAPSULE_EMIT_LEDGER` | ledger path (default `ledger.jsonl`) |
| `CAPSULE_EMIT_LITELLM_REQUEST_RECORD` | `0` to drop the chain parent |
| `CAPSULE_EMIT_LITELLM_FAILURE_PAYLOAD` | `1` to opt into un-redacted failure prompts |
| `CAPSULE_EMIT_LITELLM_MAX_PAYLOAD_CHARS` | payload bound (default `20000`) |

The two required variables raise rather than defaulting. A ledger full of
capsules stamped `operator="unknown"` is worse evidence than a proxy that refuses
to start and names the missing variable.

`listener_from_env(**overrides)` is that construction, exported so you can call
it yourself — for a second listener on a different ledger, or to override one
field while keeping the environment for the rest.

## What is and is not claimed

**The planned capsule is sealed after the call, and says so.** `effect.status =
"planned"` is the profile's carve for *"this record asserts no execution"*
(§5.2 — `derive_effect_mode` maps it to `effect_mode="not_applicable"`), which is
exactly what the request half claims. It carries no timing claim, and this
adapter does not let you infer one: every capsule stamps
`observation_mode="post_hoc_event"`, and the request half additionally carries
`request_record_provenance` spelling out that it was *derived from the completed
call's log record, not witnessed before execution*.

It cannot currently be better than that. LiteLLM dispatches no observation-only
pre-call hook: `CustomLogger.async_log_pre_api_call` is declared but has **zero
call sites** in the pinned release, and the sync `log_pre_api_call` that does
fire would put ledger I/O on the request path. A test pins the dead hook, so the
day litellm starts dispatching it this adapter finds out and can seal a genuine
pre-execution commitment instead.

**The listener never changes what a caller sees.** It implements exactly two
hooks — `async_log_success_event` and `async_post_call_failure_hook` — and
`async_post_call_failure_hook` always returns `None`. Returning an
`HTTPException` there would rewrite the error the client receives (in
`litellm/proxy/utils.py`, the first callback to return or raise one wins).
`async_pre_call_hook` is deliberately **not** implemented and must not be added:
it is deny-capable, and denial is a gate concern, not a record concern. A test
asserts all three of those things.

**Nothing here is an enforcement layer, and nothing here claims adoption by
LiteLLM.** This is an adapter you install; it is not part of `litellm`.

## Redaction: we digest what your redaction produced

On the success path litellm runs **every** registered callback's
`async_logging_hook` to completion *before* it dispatches **any**
`async_log_success_event` (`litellm_core_utils/litellm_logging.py`, in
`Logging._async_success_handler_body`, which `async_success_handler` wraps),
threading each hook's return value into the shared `model_call_details` and
`result`. So a sibling redaction callback's output is
what reaches this listener — regardless of registration order.

That is the honest claim for a receipt: **the capsule commits to the redacted
view, the record as your pipeline produced it**, not to the wire payload. An
auditor verifies it against the same redacted log you keep. If your redaction is
wrong, the receipt faithfully proves the wrong thing; the capsule is evidence of
what was logged, not a second opinion about it.

Two consequences worth being explicit about:

**One prompt field is sealed, not all of them.** `model_call_details` carries the
same prompt under `messages`, `input` and `prompt`. litellm's own
`perform_redaction` clears all three, but a *custom* `async_logging_hook` that
rewrites only `messages` — the obvious thing to write — leaves the others holding
the original text. Sealing more than one would let the least redacted copy decide
the digest and quietly undo your redaction, so exactly one is sealed: the first
of `messages`, `input`, `prompt` that is present, named in `prompt_field` so the
preimage stays reconstructible.

**The failure path is asymmetric, and is not papered over.**
`ProxyLogging.post_call_failure_hook` dispatches `async_post_call_failure_hook`
with the raw proxy `request_data`; it does **not** route it through
`async_logging_hook` or `redact_message_input_output_from_logging`. Digesting the
prompt there would commit to an un-redacted preimage that no redacted log can
reproduce. So by default the failure path seals an allowlisted view **without**
the prompt and stamps `request_payload_withheld: true` plus the reason. Absent is
recorded as absent, never passed off as empty. Set
`CAPSULE_EMIT_LITELLM_FAILURE_PAYLOAD=1` to opt in.

## Secrets never reach the digest layer

`kwargs["litellm_params"]` carries `api_key`, `azure_password`, `client_secret`
and friends, and a response's `_hidden_params` can carry a key too. Capsules are
digest-only, so nothing here would *store* a credential — but a digest is still a
commitment to a preimage a verifier has to be handed. Only an explicit allowlist
reaches the digest layer:

- request: `model`, `call_type`, `user`, `stream`, plus one prompt field
- response: `id`, `model`, `object`, `created`, `choices`, `usage`

`litellm_params` and `optional_params` are not on it and must not be added. A
test asserts no credential-shaped key is in the allowlist and that none of four
planted secrets reaches the ledger.

## A listener failure cannot fail your proxy

Every seal is wrapped: failures warn (`RuntimeWarning`) and are skipped. A broken
ledger or a dead anchor endpoint must not turn into a 500 for someone's chat
request. When the planned capsule cannot be sealed, the outcome capsule is still
written and carries `unchained_reason` — a reader can tell "this outcome has no
recorded request, and here is why" from the ledger alone, instead of seeing an
unremarkable record with a null chain.

Raw floats in a payload canonicalize at the `adapters._base` funnel (RFC 8785
§3.2.2.3 decimal strings), so an ordinary `cost: 0.5` seals and the two records
chain. A payload with no canonical form at all — NaN, ±Infinity — still fails
closed, loudly, without taking the request down.

## Payload bound

`max_payload_chars` (default 20000) bounds the sealed prompt and the response
`choices`. Over the bound, the value is **replaced** by a recorded marker
(`{"capsule_emit_truncated": true, "original_repr_chars": N}`) rather than
silently shortened, so a clipped digest can never be mistaken for a faithful one.
The identifying scalars — model, call type, response id, usage — are never
truncated, so an oversized call still produces a receipt that says which model
ran. `0` disables the bound.

## Coverage gap, stated

This adapter consumes the proxy's `async_post_call_failure_hook`. On the **pure
SDK** path (`litellm.completion` / `acompletion` with no proxy in front),
failures surface through `async_log_failure_event` instead, which this adapter
does **not** implement — so an SDK-only failure seals no capsule at all. That is
a deliberate scope boundary, not a check that passed. If you need SDK-side
failures recorded, that is a one-method addition and should be asked for.

## Testing without litellm

All sealing logic lives in `LiteLLMListenerCore`, which takes plain dicts and
imports nothing from `litellm`. The `CustomLogger` shell and the config-load path
are covered separately by tests that `importorskip("litellm")`.

```python
from capsule_emit.adapters.litellm_listener import LiteLLMListenerCore

core = LiteLLMListenerCore(operator="acme-co", developer="gateway@v1")
core.on_success_core({"model": "gpt-4o-mini", "call_type": "acompletion"}, {"id": "r1"})
```

## Quickstart

```bash
pip install "capsule-emit[litellm]"
python examples/litellm-listener/demo.py
```

The demo is hermetic — a stub SCITT TS, `litellm`'s own `mock_response` path, no
network and no LLM key — and it loads the listener through
`initialize_callbacks_on_proxy`, the real config path, rather than by importing
it directly.

## Version

Verified against the released `litellm==1.99.0` wheel; the `[litellm]` extra pins
`litellm>=1.99.0`.

**That wheel declares `Requires-Python: >=3.10,<3.15` but imports
`typing.NotRequired`, which is 3.11+.** On CPython 3.10 a plain `import litellm`
raises `ImportError: cannot import name 'NotRequired' from 'typing'` — before any
capsule-emit code runs. capsule-emit's own floor is 3.9 and is unchanged; this
adapter simply needs 3.11+ in practice, and its litellm-backed tests skip below
that. Reported here as an observation about the released artifact, not as a
capsule-emit limitation.
