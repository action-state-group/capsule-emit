# SPDX-License-Identifier: Apache-2.0
"""Build a seal request for one agent task's conversation.

``capsule-emit``'s adapters seal one record per tool call and never keep message
text. A report that asks "did the person write before the agent acted, did every
action get a result, did the agent say how it ended" needs the whole task in one
record. :func:`task_seal_request` builds that record as a ``capsule-seal-request/v1``
document, which ``capsulectl publish --request`` seals into a book. Nothing is
sealed here and nothing is sealed unless the developer calls this.

The payload is ``{"case": {...}, "agent_interaction": {"mode": ..., "messages": [...]}}``.
``mode`` says what the record discloses, so a report can show the receiver:

- ``structure`` (the default): per message only its role, whether it is
  non-empty, the id of each tool call it makes and the id a tool message
  answers. No text, no tool name, no argument, no digest of any of them: a
  plain hash of a short reply or a small argument can be guessed by hashing
  candidates, so none is sealed. Order is the order of the list.
- ``full_text`` (``full_text=True``, one task at a time): the message text, tool
  names, arguments and results are sealed as given.

The builder checks the shape of the conversation, not its completeness: a call
with no result, or a result before its call, is built as it happened, because
finding that out is the reader's check.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable
from typing import Any

from .numbers import FloatInDigestError, canonicalize_for_digest

__all__ = ["MODE_FULL_TEXT", "MODE_STRUCTURE", "TaskRequestError", "task_seal_request"]

MODE_STRUCTURE = "structure"
MODE_FULL_TEXT = "full_text"
_ROLES = ("system", "user", "assistant", "tool")
_NAME = re.compile(r"[A-Za-z0-9._-]+\Z")


class TaskRequestError(ValueError):
    """The conversation or an identity argument is not in the accepted shape."""


def _nonempty(content: Any) -> bool:
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):  # OpenAI-style content parts
        return any(isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip() for p in content)
    return bool(content)


def _calls(index: int, message: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise TaskRequestError(f"message {index}: a tool call must be an object")
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise TaskRequestError(f"message {index}: a tool call needs a non-empty string id")
        out.append(
            {"id": call_id, "name": call.get("name", fn.get("name")), "arguments": call.get("arguments", fn.get("arguments"))}
        )
    return out


def _message(index: int, message: Any, full_text: bool) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise TaskRequestError(f"message {index}: must be an object")
    role = message.get("role")
    if role not in _ROLES:
        raise TaskRequestError(f"message {index}: role must be one of {', '.join(_ROLES)}, got {role!r}")
    calls = _calls(index, message)
    if calls and role != "assistant":
        raise TaskRequestError(f"message {index}: only an assistant message makes tool calls")
    answers = message.get("tool_call_id")
    if role == "tool":
        if not isinstance(answers, str) or not answers:
            raise TaskRequestError(f"message {index}: a tool message needs the non-empty string tool_call_id it answers")
    elif answers is not None:
        raise TaskRequestError(f"message {index}: only a tool message has a tool_call_id")
    content = message.get("content")
    out: dict[str, Any] = {"role": role, "nonempty": _nonempty(content)}
    if full_text:
        out["content"] = content
    if calls:
        out["tool_calls"] = calls if full_text else [{"id": c["id"]} for c in calls]
    if answers is not None:
        out["tool_call_id"] = answers
    return out


def _timestamp(value: str | dt.datetime | None) -> str:
    if value is None:
        value = dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise TaskRequestError(f"timestamp {value!r} is not an RFC 3339 time") from None
    if value.tzinfo is None:
        raise TaskRequestError("timestamp needs a time zone")
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_seal_request(
    messages: Iterable[dict[str, Any]],
    *,
    suite: str,
    domain: str,
    task_id: str | int,
    operator: str,
    developer: str,
    trial: int = 0,
    full_text: bool = False,
    timestamp: str | dt.datetime | None = None,
) -> dict[str, Any]:
    """The ``capsule-seal-request/v1`` document for one task's conversation.

    ``suite``, ``domain``, ``task_id`` and ``trial`` name the task: the case is
    ``suite:domain:task-<task_id>:trial-<trial>``, so give each run of the same
    task its own ``trial``. ``suite`` and ``domain`` are limited to letters,
    digits, ``.``, ``_`` and ``-``. ``messages`` are OpenAI-style: ``role``,
    ``content``, an assistant's ``tool_calls`` (``id`` and ``name``/``arguments``
    or ``function``) and a tool message's ``tool_call_id``.
    """
    for label, value in (("suite", suite), ("domain", domain)):
        if not isinstance(value, str) or not _NAME.match(value):
            raise TaskRequestError(f"{label} must match [A-Za-z0-9._-]+, got {value!r}")
    if isinstance(task_id, bool) or not isinstance(task_id, (str, int)) or str(task_id) == "":
        raise TaskRequestError("task_id must be a non-empty string or an integer")
    if isinstance(trial, bool) or not isinstance(trial, int) or trial < 0:
        raise TaskRequestError("trial must be a non-negative integer")
    for label, value in (("operator", operator), ("developer", developer)):
        if not isinstance(value, str) or not value:
            raise TaskRequestError(f"{label} must be a non-empty string")
    sealed = [_message(i, m, full_text) for i, m in enumerate(messages)]
    if not sealed:
        raise TaskRequestError("the conversation has no messages")
    interaction = {"mode": MODE_FULL_TEXT if full_text else MODE_STRUCTURE, "messages": sealed}
    if full_text:  # tool arguments and results may hold floats, which a sealed payload cannot
        try:
            interaction = canonicalize_for_digest(json.loads(json.dumps(interaction)))
        except FloatInDigestError as exc:  # NaN or an infinity cannot be sealed
            raise TaskRequestError(str(exc)) from exc
    return {
        "spec_version": "capsule-seal-request/v1",
        "capsule": {
            "ActionID": f"urn:agent-task:{suite}:{domain}:task-{task_id}:trial-{trial}",
            "ActionType": "fyi",
            "Operator": operator,
            "Developer": developer,
            "Timestamp": _timestamp(timestamp),
        },
        "payload": {
            "case": {"benchmark": suite, "domain": domain, "task_id": task_id, "trial": trial},
            "agent_interaction": interaction,
        },
    }
