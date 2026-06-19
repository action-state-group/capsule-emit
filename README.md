# capsule-emit

One-call `emit()` for [Agent Action Capsules](https://github.com/action-state-group/agent-action-capsule) — anchor on by default, ledger view CLI, thin framework adapters.

## Install

```bash
pip install capsule-emit                 # emit + anchor-client + ledger CLI
pip install "capsule-emit[langchain]"    # + LangChain callback adapter
```

## Quickstart (~15 lines)

```python
from capsule_emit import emit

cap = emit(
    action="write_po",
    operator="acme-co",
    developer="po-agent@v1",
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
    agent_output=result,
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    verdict="executed",
    effect={"type": "write_po", "status": "dispatched"},
)
print(cap.capsule_id, cap.anchored)  # sealed + anchored
```

See [capsule-emit-quickstart.md](https://github.com/action-state-group/agent-action-capsule/blob/main/docs/capsule-emit-quickstart.md) for the full 5-minute walkthrough.

## Ledger view

```bash
capsule-emit ledger view ./ledger.jsonl
```

## Verify

```bash
python -m agent_action_capsule verify ./ledger.jsonl
```

## License

BSD-3-Clause
