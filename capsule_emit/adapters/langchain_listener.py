# SPDX-License-Identifier: Apache-2.0
"""LangChain callback listener — planned/confirmed/failed capsules per tool call.

    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    listener = LangChainCapsuleListener(operator="acme-co", developer="my-agent@v1")
    agent.invoke(..., config={"callbacks": [listener]})

Requires ``pip install "capsule-emit[langchain]"`` (langchain-core).

This is the listener-grade upgrade of
:class:`~capsule_emit.adapters.langchain.LangChainCapsuleEmitter` (which stays
for surgical single-capsule-per-call use). Differences, mirroring the CrewAI
listener design:

- ``on_tool_start``  → **planned** capsule (the commitment record)
- ``on_tool_end``    → **confirmed** capsule, ``confirms``-chained to the
  planned one (did → confirmed; response digest auto-derived)
- ``on_tool_error``  → **errored/failed** capsule, chained — errors become
  evidence instead of being dropped
- root ``on_chain_start/end/error`` → fyi lifecycle capsules (config-gated,
  root runs only so nested runnables don't flood the ledger)
- LLM call events → OFF by default (``include_llm=True`` to enable); model
  info is still auto-captured and threaded into the next tool capsule

Pairing uses LangChain's ``run_id`` (exact, no FIFO heuristics needed).
There is no replay concept in LangChain's callback system, so no replay
guard is wired (CrewAI's listener has one because its bus documents replay);
content-digest idempotence is the backstop if a caller re-runs a chain.

A handler never raises: LangChain's callback manager already suppresses
handler exceptions by default (``raise_error=False``), and every seal here
additionally warns-instead-of-raising, so a broken anchor endpoint cannot
affect the host application.

Floats in tool payloads are canonicalized before they reach the digest layer
(RFC 8785 §3.2.2.3 decimal strings, via
:func:`capsule_emit.numbers.canonicalize_for_digest` in
``adapters._base.emit_capsule``), so an ordinary ``value: float`` tool
argument seals normally and its two records chain. What remains
unsealable is a payload with no canonical form at all — NaN, ±Infinity, an
object JSON cannot carry — or a genuinely broken ledger. Those still warn and
seal no planned capsule, because warn-and-skip is the only agent-safe choice
here: raising would surface a capsule-emit problem as the host application's
tool failure.

Skipping the planned record must not, however, leave a *silent* orphan. The
run_id stays in the pending map with a ``None`` id, so the outcome record
keeps the real tool name and carries ``unchained_reason`` in its compute
attestation — a reader can tell "this outcome has no committed plan, and
here is why" from the ledger alone, instead of seeing an unremarkable
confirmed record with ``chain: null`` (capsule-emit#128).

All sealing logic lives in the framework-free :class:`LangChainListenerCore`
(fully testable without langchain installed); the shell class binds it to
``langchain_core.callbacks.BaseCallbackHandler``.
"""
from __future__ import annotations

import warnings
from collections import OrderedDict
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = ["LangChainCapsuleListener", "LangChainListenerCore"]

#: Stamped on an outcome capsule whose planned capsule could not be sealed, so
#: the missing chain link is self-describing in the ledger rather than silent.
UNCHAINED_REASON = (
    "planned capsule could not be sealed for this tool call; this outcome "
    "record has no parent and is not evidence of a committed plan"
)


def _extract_model_from_serialized(serialized: dict | None) -> dict[str, str] | None:
    """Pull provider + model_id from a LangChain serialized LLM dict.

    Pure dict handling (duplicated from ``adapters.langchain`` so this module
    stays importable without langchain-core; keep the two in sync).
    """
    if not serialized:
        return None
    kw = serialized.get("kwargs") or {}
    model_id = kw.get("model_name") or kw.get("model") or kw.get("model_id")
    name = serialized.get("name")
    if not model_id and not name:
        ident = serialized.get("id")
        if isinstance(ident, (list, tuple)) and ident:
            name = str(ident[-1])
    if not model_id and not name:
        return None
    provider = None
    probe = (name or "").lower()
    for needle, prov in (
        ("anthropic", "anthropic"),
        ("openai", "openai"),
        ("google", "google"),
        ("gemini", "google"),
        ("bedrock", "aws"),
        ("ollama", "ollama"),
    ):
        if needle in probe:
            provider = prov
            break
    return {
        "provider": provider or "unknown",
        "model_id": str(model_id or name),
    }


class LangChainListenerCore(CapsuleEmitterBase):
    """Framework-free core of the LangChain callback listener.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - tool start  → ``effect.status="planned"``
    - tool end    → ``effect.status="confirmed"``, chained to the planned id
    - tool error  → ``verdict="errored"``, ``effect.status="failed"``, chained
    - root chain start/end/error → fyi capsules (``include_lifecycle``)
    - LLM events  → gated by ``include_llm`` (default off; volume, not evidence)

    Args:
        include_lifecycle: Seal root-run chain start/end/error as fyi capsules
            (default True; root runs only — ``parent_run_id is None``).
        include_llm: Seal LLM call events themselves (default False). Model
            auto-capture into tool capsules happens regardless.
        max_pending: Bound on remembered planned capsules keyed by run_id
            (default 256) so starts without ends cannot grow without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer,
            ledger, anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        include_lifecycle: bool = True,
        include_llm: bool = False,
        max_pending: int = 256,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._include_lifecycle = include_lifecycle
        self._include_llm = include_llm
        self._max_pending = max_pending
        # run_id -> (tool_name, planned_capsule_id); insertion-ordered for bound-eviction
        self._pending: OrderedDict[Any, tuple[str, str]] = OrderedDict()
        self._captured_model: dict[str, str] | None = None

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the host app)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: LangChain listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    def _take_model(self) -> dict[str, str] | None:
        model, self._captured_model = self._captured_model, None
        return model

    # -- LLM / chat model start: model auto-capture ------------------------

    def on_llm_start_core(self, serialized: dict | None) -> None:
        captured = _extract_model_from_serialized(serialized)
        if captured:
            self._captured_model = captured
        if self._include_llm:
            self._seal(
                action="llm_call_started",
                tool_input={"model": str((captured or {}).get("model_id"))},
                action_type="fyi",
                runtime="langchain",
            )

    # -- tool events -------------------------------------------------------

    def on_tool_start_core(
        self, serialized: dict | None, tool_input: Any, run_id: Any
    ) -> None:
        tool_name = (serialized or {}).get("name") or "tool"
        result = self._seal(
            action=tool_name,
            tool_input=tool_input,
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime="langchain",
            model=self._captured_model,
        )
        while len(self._pending) >= self._max_pending:
            self._pending.popitem(last=False)  # evict oldest
        # Recorded even when the seal failed (id None): the outcome handler
        # needs the real tool name, and needs to know a plan was attempted and
        # lost rather than never started — see UNCHAINED_REASON.
        self._pending[run_id] = (tool_name, None if result is None else result.capsule_id)

    def _resolve_pending(self, run_id: Any) -> tuple[str, str | None, bool]:
        """Return ``(tool_name, planned_id, planned_was_dropped)`` for an outcome.

        An unknown ``run_id`` (an end without a start) is not a dropped plan —
        there is nothing to have lost — so it gets no ``unchained_reason``.
        """
        entry = self._pending.pop(run_id, None)
        if entry is None:
            return "tool", None, False
        tool_name, planned_id = entry
        return tool_name, planned_id, planned_id is None

    @staticmethod
    def _unchained_compute(dropped: bool) -> dict[str, Any] | None:
        return {"unchained_reason": UNCHAINED_REASON} if dropped else None

    def on_tool_end_core(self, output: Any, run_id: Any) -> None:
        tool_name, planned_id, dropped = self._resolve_pending(run_id)
        self._seal(
            action=tool_name,
            tool_output=output,
            effect={"type": tool_name, "status": "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="langchain",
            model=self._take_model(),
            extra_compute=self._unchained_compute(dropped),
        )

    def on_tool_error_core(self, error: BaseException, run_id: Any) -> None:
        tool_name, planned_id, dropped = self._resolve_pending(run_id)
        self._seal(
            action=tool_name,
            tool_output=None if error is None else str(error),
            verdict="errored",
            effect={"type": tool_name, "status": "failed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="langchain",
            model=self._take_model(),
            extra_compute=self._unchained_compute(dropped),
        )

    # -- root chain lifecycle ---------------------------------------------

    def on_chain_lifecycle_core(
        self, phase: str, payload: Any, run_id: Any, parent_run_id: Any
    ) -> None:
        """Root-run chain start/end/error → one fyi capsule.

        ``phase`` ∈ {"started", "completed", "failed"}. Non-root runs
        (``parent_run_id`` not None) are ignored — every nested runnable
        fires chain callbacks, and lifecycle evidence is per-invocation.
        """
        if not self._include_lifecycle or parent_run_id is not None:
            return
        self._seal(
            action=f"chain_{phase}",
            tool_input=None if payload is None else {"summary": str(payload)[:2000]},
            verdict="errored" if phase == "failed" else "executed",
            action_type="fyi",
            runtime="langchain",
        )


try:
    from langchain_core.callbacks import BaseCallbackHandler as _Base

    _HAVE_LANGCHAIN = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _Base = object  # type: ignore[assignment,misc]
    _HAVE_LANGCHAIN = False


class LangChainCapsuleListener(_Base):  # type: ignore[valid-type,misc]
    """``BaseCallbackHandler`` shell binding LangChain callbacks to the core.

    Register via ``config={"callbacks": [listener]}`` on any ``invoke()`` /
    agent run, or globally per LangChain's callback docs.
    """

    # LangChain honors this flag: never re-raise handler exceptions.
    raise_error = False
    run_inline = True  # seal in-order relative to the run

    def __init__(self, **core_kw: Any) -> None:
        if not _HAVE_LANGCHAIN:
            raise ImportError(
                "LangChainCapsuleListener needs langchain-core. "
                'Install with: pip install "capsule-emit[langchain]"'
            )
        super().__init__()
        self.core = LangChainListenerCore(**core_kw)

    # LLM / chat model — model auto-capture (+ optional llm capsules)
    def on_llm_start(self, serialized: dict | None, prompts: Any, **kw: Any) -> None:
        self.core.on_llm_start_core(serialized)

    def on_chat_model_start(
        self, serialized: dict | None, messages: Any, **kw: Any
    ) -> None:
        self.core.on_llm_start_core(serialized)

    # Tools
    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any = None,
        inputs: dict | None = None,
        **kw: Any,
    ) -> None:
        self.core.on_tool_start_core(
            serialized, inputs if inputs is not None else input_str, run_id
        )

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kw: Any) -> None:
        self.core.on_tool_end_core(output, run_id)

    def on_tool_error(
        self, error: BaseException, *, run_id: Any = None, **kw: Any
    ) -> None:
        self.core.on_tool_error_core(error, run_id)

    # Root chain lifecycle
    def on_chain_start(
        self,
        serialized: dict | None,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kw: Any,
    ) -> None:
        self.core.on_chain_lifecycle_core("started", inputs, run_id, parent_run_id)

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kw: Any,
    ) -> None:
        self.core.on_chain_lifecycle_core("completed", outputs, run_id, parent_run_id)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kw: Any,
    ) -> None:
        self.core.on_chain_lifecycle_core("failed", error, run_id, parent_run_id)
