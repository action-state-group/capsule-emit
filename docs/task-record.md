# Seal one agent task's conversation

The adapters seal one record per tool call. A report that asks whether the person wrote before the agent acted, whether every action got a result, and whether the agent said how it ended needs the whole task in one record. `capsule-emit task-request` builds that record as a `capsule-seal-request/v1` file, and `capsulectl publish --request` seals it into your book. Nothing is sealed unless you run it.

## Two commands

`conv.json` is the task's conversation, OpenAI-style: a list of messages with `role` (`system`, `user`, `assistant`, `tool`), `content`, an assistant's `tool_calls` (`id`, and `name` and `arguments`, or a `function` object) and a tool message's `tool_call_id`.

```sh
capsule-emit task-request --messages conv.json \
  --suite my-evals --domain po-agent --task-id t1 \
  --operator acme --developer po-agent@v1 -o req.json
capsulectl publish --profile mine --request req.json
```

The profile is one you created with `capsulectl profile create` and `capsulectl store init`. The first command prints `wrote req.json (4 messages, mode structure)` and the second prints `"state":"appended"`.

`--suite`, `--domain`, `--task-id` and `--trial` (default 0) name the case, `suite:domain:task-<task_id>:trial-<trial>`: give each run of the same task its own trial. `--timestamp` is the task's RFC 3339 time (default: now); with `--timestamp` set, the same request published again gives the same capsule.

## What the record discloses

The payload is `{"case": ..., "agent_interaction": {"mode": ..., "messages": [...]}}`, and `mode` says which of these you sealed, so a report shows the receiver what was disclosed.

| Mode | Per message | Use it when |
|---|---|---|
| `structure` (default) | `role`, `nonempty`, a call ref for each tool call and the call ref a tool message answers | The report only needs to know the conversation's shape. |
| `full_text` (`--full-text`, one task at a time) | the same, plus `content`, and each call's `name` and `arguments` | A reader must judge what was said or done. |

In `structure` mode a call ref is the ordinal of the call id's first appearance (`"0"`, `"1"`, ...), not the id itself, because some servers build ids from the tool name; two calls that share an id share a ref, so a duplicate stays visible. No text, tool name, argument or result is sealed, and no digest of any of them: a plain hash of a short reply or a small argument can be guessed by hashing candidates. In `full_text` mode a float in an argument is sealed as its decimal string.

The builder checks the shape, not completeness: a call with no result, or a result before its call, is sealed as it happened, because finding that out is the report's check. A conversation with no messages, an unknown role, a tool call with no id, or a tool message with no `tool_call_id` is refused.

From Python: `capsule_emit.task_request.task_seal_request(messages, suite=..., domain=..., task_id=..., operator=..., developer=..., full_text=False)` returns the same document.
