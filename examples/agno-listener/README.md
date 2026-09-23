# Agno Tool-Hook Listener Quickstart

Register once on the agent, every tool call seals a capsule — no per-tool code.

```
examples/agno-listener/
├── demo.py     # standalone run — real Agno tool calls, hermetic stub anchor, verify() all
└── README.md   # this file
```

## Run it

```bash
pip install "capsule-emit[agno]"
python examples/agno-listener/demo.py
```

No LLM key and no external service needed: the demo starts a hermetic local
stub SCITT Transparency Service (the same pattern the test suite uses), drives
real Agno tool calls through the hook chain — a success, a failure, and a cache
hit — then verifies every sealed capsule offline.

Expected tail of the output:

```
| # | tool           | type | verdict  | effect    | capsule_id |
| 1 | `get_price`    | fyi  | executed | planned   | 81a15ebb   |
| 2 | `get_price`    | fyi  | executed | confirmed | a0b9d44f   |
| 3 | `submit_order` | fyi  | executed | planned   | 14814d2b   |
| 4 | `submit_order` | fyi  | errored  | failed    | 9316b438   |
| 5 | `get_price`    | fyi  | executed | planned   | aa270582   |
| 6 | `get_price`    | fyi  | executed | confirmed | a772cb20   |

all capsules verify offline; evidence renders clean. done.
```

Each tool call seals a `planned` commitment and chains its outcome to it —
`confirmed` when the tool returned, `failed` when it raised. The pairing is what
makes a refusal or a crash visible rather than simply absent.

One nuance worth knowing: Agno's MCP entrypoints are coroutines, so wire
`listener.async_hook` (not the sync `listener.hook`) for tool calls driven
through `arun`/`aexecute`. Both kinds of `Function` run the same hook chain —
there is no MCP-specific bypass.

Registering, in full:

```python
from capsule_emit.adapters.agno_listener import AgnoCapsuleListener

listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent = Agent(model=..., tools=[...], tool_hooks=[listener.hook])
```

See [`docs/adapters/agno.md`](../../docs/adapters/agno.md) for what the record
does and does not prove.
