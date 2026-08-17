# CrewAI Event-Bus Listener Quickstart

Register once, every tool call in the crew seals a capsule — no per-tool code.

```
examples/crewai-listener/
├── demo.py     # standalone run — real crewai event bus, hermetic stub anchor, verify() all
└── README.md   # this file
```

## Run it

```bash
pip install "capsule-emit[crewai]"
python examples/crewai-listener/demo.py
```

No LLM key and no external service needed: the demo starts a hermetic local
stub SCITT Transparency Service (the same pattern the test suite uses), drives
CrewAI's real event bus through a realistic crew-run event sequence, then
verifies every sealed capsule offline with `agent_action_capsule.verify()`.

Expected tail of the output:

```
action                             effect     verdict   chained  verify()
---------------------------------------------------------------------------
crew_kickoff_started/...           -          executed  -        OK
fetch_supplier/...                 planned    executed  -        OK
fetch_supplier/...                 confirmed  executed  yes      OK
write_po/...                       planned    executed  -        OK
write_po/...                       failed     errored   yes      OK
crew_kickoff_completed/...         -          executed  -        OK
---------------------------------------------------------------------------
6 capsules, verify: ALL OK
```

## In a real app

```python
from capsule_emit.adapters.crewai_listener import CapsuleEventListener

CapsuleEventListener(operator="acme-co", developer="ops-crew@v1")
# ...then run your crew as usual:
crew.kickoff()
```

That's the whole integration: instantiate before `kickoff()`; registration
happens in the constructor via CrewAI's standard `BaseEventListener` mechanism.
Every `ToolUsageStartedEvent` seals a *planned* capsule, every
`ToolUsageFinishedEvent` seals a *confirmed* capsule chained to it, every
`ToolUsageErrorEvent` seals an *errored/failed* capsule. Crew kickoff
lifecycle events seal as `fyi` (disable with `include_lifecycle=False`); LLM
call events are off by default (`include_llm=True` to enable).

Replay-safe (honors the bus's `is_replaying()` guidance) and isolation-safe
(a listener failure — e.g. an unreachable anchor — never affects the crew run).
