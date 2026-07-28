# SPDX-License-Identifier: Apache-2.0
"""Thin LangChain shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = LangChainCapsuleEmitter(operator="acme-co", developer="my-agent@v1")
    agent.invoke(..., config={"callbacks": [emitter]})

EU AI Act Art. 12 compliance:

    from capsule_emit.adapters.langchain import ScittAuditCallbackHandler

    handler = ScittAuditCallbackHandler(
        operator="acme-co",
        developer="invoice-agent@v1",
        risk_class="high-risk",  # EU AI Act Annex III classification
    )
    agent.invoke(..., config={"callbacks": [handler]})
    print(handler.audit_summary())

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

__all__ = ["LangChainCapsuleEmitter", "ScittAuditCallbackHandler"]

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


class ScittAuditCallbackHandler(LangChainCapsuleEmitter):
    """EU AI Act Art. 12-compliant LangChain callback handler.

    Emits one SCITT-anchored capsule per tool call, tagged with the operator's
    EU AI Act Annex III risk classification.  Every capsule is written to an
    append-only local ledger and anchored to a public SCITT Transparency
    Service, producing a tamper-evident audit trail suitable for Art. 12
    record-keeping obligations.

    Key properties vs. plain logging:
    - **Tamper-evident**: each capsule carries a SCITT receipt; the ledger is
      hash-chained and independently verifiable.
    - **PII-safe by design**: only SHA-256 digests of inputs/outputs are sealed;
      raw content never leaves the host system.
    - **Model-attributed**: the LLM provider + model_id are captured
      automatically from LangChain's ``on_llm_start`` callback and attached to
      every tool capsule.
    - **Chain-bookended**: optional ``on_chain_start`` / ``on_chain_end``
      capsules bracket the entire agent run; subsequent tool capsules reference
      the chain head via ``prior_capsule_id``.

    Args:
        operator: Tenant/org identifier (e.g. ``"acme-co"``).
        developer: Agent name + version (e.g. ``"invoice-agent@v2"``).
        risk_class: EU AI Act Annex III classification stamped on every capsule.
            Defaults to ``"high-risk"``.  Use ``"limited-risk"`` or
            ``"minimal-risk"`` for non-Annex-III deployments.
        emit_chain_events: When ``True`` (default), emit bookend capsules at
            top-level chain start/end so the audit trail covers the full agent
            run, not just individual tool calls.
        **kwargs: Forwarded to :class:`LangChainCapsuleEmitter` (``ledger``,
            ``anchor``, ``anchor_url``, ``model``, etc.).

    Usage::

        handler = ScittAuditCallbackHandler(
            operator="acme-co",
            developer="invoice-agent@v2",
            risk_class="high-risk",
        )
        result = agent.invoke({"input": "..."}, config={"callbacks": [handler]})
        print(handler.audit_summary())
    """

    def __init__(
        self,
        *,
        risk_class: str = "high-risk",
        emit_chain_events: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._risk_class = risk_class
        self._emit_chain_events = emit_chain_events
        self._chain_head: str | None = None

    # ------------------------------------------------------------------
    # Chain bookends (top-level only; nested chains are skipped)
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict | None,
        inputs: dict,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kw: Any,
    ) -> None:
        if self._emit_chain_events and parent_run_id is None:
            result = self.emit_capsule(
                "chain-start",
                tool_input=inputs,
                tool_output=None,
                verdict="started",
                action_type=self._risk_class,
            )
            self._chain_head = result.capsule_id

    def on_chain_end(
        self,
        outputs: dict,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kw: Any,
    ) -> None:
        if self._emit_chain_events and parent_run_id is None:
            self.emit_capsule(
                "chain-end",
                tool_input=None,
                tool_output=outputs,
                verdict="completed",
                action_type=self._risk_class,
                prior_capsule_id=self._chain_head,
            )
            self._chain_head = None

    # ------------------------------------------------------------------
    # Tool callbacks — emit risk-tagged capsule
    # ------------------------------------------------------------------

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kw: Any) -> None:
        name, inp = self._pending.pop(run_id, ("tool", None))
        model = self._captured_model
        self._captured_model = None
        self.emit_capsule(
            name,
            tool_input=inp,
            tool_output=output,
            model=model,
            action_type=self._risk_class,
            prior_capsule_id=self._chain_head,
        )

    # ------------------------------------------------------------------
    # Audit summary (Art. 12 disclosure helper)
    # ------------------------------------------------------------------

    def audit_summary(self) -> dict:
        """Return a dict suitable for an EU AI Act Art. 12 audit disclosure.

        Includes capsule count, SCITT anchor status, risk classification, and
        the list of capsule IDs that form the tamper-evident chain.
        """
        capsules = self.results
        return {
            "risk_class": self._risk_class,
            "operator": self._operator,
            "developer": self._developer,
            "capsule_count": len(capsules),
            "anchored_count": sum(1 for r in capsules if r.anchored),
            "capsule_ids": [r.capsule_id for r in capsules],
            "ledger": str(self._ledger),
        }
