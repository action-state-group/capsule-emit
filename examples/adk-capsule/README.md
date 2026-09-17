# Google ADK adapter demo

Hermetic: no LLM key, no live service. Drives both wiring paths with real ADK
objects — `FunctionTool` through the tool callbacks, `Event` objects through
the event-stream tap — plus the guard (a refusal in the record) and
an `on_tool_error_callback` routed to `emit_errored` (a raise in the record), then verifies every capsule offline.

```bash
pip install "capsule-emit[adk]"
python examples/adk-capsule/demo.py
```
