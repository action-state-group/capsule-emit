# SPDX-License-Identifier: Apache-2.0
"""Thin LangChain shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = LangChainCapsuleEmitter(operator="acme-co", developer="my-agent@v1")
    agent.invoke(..., config={"callbacks": [emitter]})

Requires ``pip install langchain-core``.

Model auto-capture: ``on_llm_start`` is called by LangChain before each LLM
invocation and carries model info in the ``serialized`` dict.  The adapter
captures it and threads it into the next tool capsule automatically — no need
to pass ``model=`` by hand when the chain runs through LangChain's callback
system.
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


def _extract_model_from_serialized(serialized: dict | None) -> dict[str, str] | None:
    """Pull provider + model_id from a LangChain serialized LLM dict.

    LangChain passes different shapes for different providers:
    - ``serialized["kwargs"]["model_name"]``  (OpenAI, Anthropic, most)
    - ``serialized["kwargs"]["model"]``       (some Anthropic configs)
    - ``serialized["name"]``                  (friendly class name, e.g. "ChatAnthropic")
    - ``serialized["id"][-1]``               (class-path tail, fallback)

    Provider is inferred from the class name when not explicit.
    """
    if not serialized:
        return None

    kw = serialized.get("kwargs") or {}
    model_id = kw.get("model_name") or kw.get("model") or kw.get("model_id")

    class_name = serialized.get("name") or (
        serialized.get("id", [""])[-1] if serialized.get("id") else ""
    )
    class_lower = class_name.lower()

    if "openai" in class_lower:
        provider = "openai"
    elif "anthropic" in class_lower:
        provider = "anthropic"
    elif "google" in class_lower or "gemini" in class_lower:
        provider = "google"
    elif "cohere" in class_lower:
        provider = "cohere"
    elif "mistral" in class_lower:
        provider = "mistral"
    elif "ollama" in class_lower:
        provider = "ollama"
    else:
        provider = class_lower or "unknown"

    if not model_id and not class_name:
        return None

    return {"provider": provider, "model_id": model_id or class_name}


class LangChainCapsuleEmitter(CapsuleEmitterBase, _Base):
    """LangChain callback handler — emits one capsule per completed tool call.

    Model auto-capture: when LangChain fires ``on_llm_start`` the adapter
    captures the model info and attaches it to the next tool capsule.  Falls
    back to the ``model=`` passed at construction time when the framework does
    not expose a model (e.g. pure tool chains without an LLM node).
    """

    def __init__(self, **kwargs: Any) -> None:
        CapsuleEmitterBase.__init__(self, **kwargs)
        _Base.__init__(self)
        self._pending: dict[Any, tuple[str, Any]] = {}
        self._captured_model: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # LLM callbacks — auto-capture model
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict | None,
        prompts: list[str],
        *,
        run_id: Any = None,
        **kw: Any,
    ) -> None:
        captured = _extract_model_from_serialized(serialized)
        if captured:
            self._captured_model = captured

    def on_chat_model_start(
        self,
        serialized: dict | None,
        messages: list,
        *,
        run_id: Any = None,
        **kw: Any,
    ) -> None:
        captured = _extract_model_from_serialized(serialized)
        if captured:
            self._captured_model = captured

    # ------------------------------------------------------------------
    # Tool callbacks — emit capsule
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any = None,
        inputs: dict | None = None,
        **kw: Any,
    ) -> None:
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        self._pending[run_id] = (name, inputs if inputs is not None else input_str)

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kw: Any) -> None:
        name, inp = self._pending.pop(run_id, ("tool", None))
        model = self._captured_model
        self._captured_model = None
        self.emit_capsule(name, tool_input=inp, tool_output=output, model=model)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None, **kw: Any) -> None:
        self._pending.pop(run_id, None)
