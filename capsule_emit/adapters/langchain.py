# SPDX-License-Identifier: BSD-3-Clause
"""Thin LangChain shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = LangChainCapsuleEmitter(operator="acme-co", developer="my-agent@v1")
    agent.invoke(..., config={"callbacks": [emitter]})

Requires ``pip install langchain-core``.
"""
from __future__ import annotations

from typing import Any

from ._base import CapsuleEmitterBase

__all__ = ["LangChainCapsuleEmitter"]

try:
    from langchain_core.callbacks import BaseCallbackHandler as _Base
except ImportError as exc:
    raise ImportError(
        "LangChainCapsuleEmitter needs langchain-core. "
        "Install with: pip install langchain-core"
    ) from exc


class LangChainCapsuleEmitter(CapsuleEmitterBase, _Base):
    """LangChain callback handler — emits one capsule per completed tool call."""

    def __init__(self, **kwargs: Any) -> None:
        CapsuleEmitterBase.__init__(self, **kwargs)
        _Base.__init__(self)
        self._pending: dict[Any, tuple[str, Any]] = {}

    def on_tool_start(self, serialized: dict | None, input_str: str, *, run_id: Any = None, inputs: dict | None = None, **kw: Any) -> None:
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        self._pending[run_id] = (name, inputs if inputs is not None else input_str)

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kw: Any) -> None:
        name, inp = self._pending.pop(run_id, ("tool", None))
        self.emit_capsule(name, tool_input=inp, tool_output=output)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None, **kw: Any) -> None:
        self._pending.pop(run_id, None)
