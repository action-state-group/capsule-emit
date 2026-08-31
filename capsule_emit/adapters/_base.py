# SPDX-License-Identifier: Apache-2.0
"""Shared base for all capsule-emit framework adapters.

All framework adapters (MCP, LangChain, CrewAI, Hermes, Goose, ADK) extend this
base. It holds operator/developer/ledger config and exposes a single
``emit_capsule()`` helper that calls the internal ``capsule_emit.core._emit_capsule``
primitive (the same one ``seal()``/``received()`` wrap).

This is also the adapters' canonicalization boundary. ``emit_capsule()`` runs
tool payloads through :func:`capsule_emit.numbers.canonicalize_for_digest`
before they reach the digest layer, so a raw ``float`` arriving from a
framework's tool schema is committed as its RFC 8785 §3.2.2.3 decimal string
rather than failing the seal. §5.1 still forbids raw floats in digest-bearing
fields — nothing is loosened; the adapter now does the deterministic
serialization the rule requires, at the one place every adapter passes
through. The core ``seal()``/``received()`` surface is deliberately left
strict: a direct caller owns its data and should hear about a float, while an
adapter is handed whatever a third-party framework decoded and cannot ask it
for decimal strings.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any

from capsule_emit.core import EmitResult, _emit_capsule
from capsule_emit.numbers import canonicalize_for_digest

__all__ = ["CapsuleEmitterBase"]


class CapsuleEmitterBase:
    """Shared config carrier for capsule-emit framework adapters.

    Args:
        operator: Tenant/org identifier stamped on every capsule.
        developer: Agent name + version.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor: Legacy, non-default fire-and-forget anchor channel. ``None``
            (default) resolves via ``CAPSULE_ANCHOR`` (off unless it is the
            exact value ``"legacy-on"``) — see ``capsule_emit.core._emit_capsule``'s
            ``anchor`` param. Pass an explicit ``True``/``False`` to override
            the env var for this instance.
        anchor_url: Override anchor endpoint (else ``AAC_ANCHOR_URL`` env var).
        anchor_wait: When set, block up to this many seconds per emit for the
            real anchor outcome, so ``EmitResult.anchored`` / ``.anchor_status``
            reflect a genuine confirmation instead of just "submitted".
            ``None`` (default) never blocks — see ``capsule_emit.core._emit_capsule()``.
        model: Default ``{"provider": ..., "model_id": ...}`` applied to every capsule
            when the adapter cannot auto-capture the model from the framework. Can be
            overridden per-emit by passing ``model=`` to :meth:`emit_capsule`.
        max_results: Cap on how many recent :class:`EmitResult` objects :attr:`results`
            retains. ``None`` (default) keeps every result — fine for short-lived use,
            but a long-running streaming adapter (e.g. ADK's event tap) should set a cap
            so the in-memory history does not grow without bound. ``0`` retains nothing.
            ``last`` is tracked independently and is always the most recent emit
            regardless of this cap.
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        ledger: str | os.PathLike = "ledger.jsonl",
        anchor: bool | None = None,
        anchor_url: str | None = None,
        anchor_wait: float | None = None,
        model: dict[str, str] | None = None,
        max_results: int | None = None,
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._anchor_url = anchor_url
        self._anchor_wait = anchor_wait
        self._default_model = model
        self._last: EmitResult | None = None
        # A bounded deque when a cap is set (streaming adapters), else an unbounded
        # list (backward-compatible default). Both support append + list().
        # `is not None` so max_results=0 means "retain nothing", not "unbounded".
        self._results: deque[EmitResult] | list[EmitResult] = (
            deque(maxlen=max_results) if max_results is not None else []
        )

    @property
    def last(self) -> EmitResult | None:
        """The most recent EmitResult, or None."""
        return self._last

    @property
    def results(self) -> list[EmitResult]:
        """All EmitResults emitted this session."""
        return list(self._results)

    def emit_capsule(
        self,
        action: str,
        tool_input: Any = None,
        tool_output: Any = None,
        *,
        verdict: str = "executed",
        effect: dict[str, Any] | None = None,
        prior_capsule_id: str | None = None,
        model: dict[str, str] | None = None,
        human_disposed: bool = False,
        approver: str = "policy",
        decision: str = "accept",
        relation: str | None = "confirms",
        action_type: str | None = None,
        runtime: str | None = None,
        extra_compute: dict[str, Any] | None = None,
    ) -> EmitResult:
        """Emit one capsule for a completed tool call.

        ``model`` falls back to the instance-level ``_default_model`` when not
        supplied, which itself is set by ``model=`` in the constructor or overridden
        per-call by framework adapters that auto-capture the model (e.g. LangChain).

        ``tool_input`` and ``tool_output`` — the two digest-bearing payload
        fields — are canonicalized first (floats → RFC 8785 decimal strings).
        Float-free payloads pass through byte-identical, so no existing digest
        moves. NaN/±Infinity have no JCS representation and raise
        ``FloatInDigestError`` from here; listeners catch it at their boundary
        and degrade loudly rather than breaking the host application.
        """
        result = _emit_capsule(
            action=action,
            operator=self._operator,
            developer=self._developer,
            agent_input=canonicalize_for_digest(tool_input, field="agent_input"),
            agent_output=canonicalize_for_digest(tool_output, field="agent_output"),
            verdict=verdict,
            effect=effect,
            confirms=prior_capsule_id,
            relation=relation,
            anchor=self._anchor,
            ledger=self._ledger,
            anchor_url=self._anchor_url,
            anchor_wait=self._anchor_wait,
            model=model if model is not None else self._default_model,
            human_disposed=human_disposed,
            approver=approver,
            decision=decision,
            action_type=action_type,
            runtime=runtime,
            extra_compute=extra_compute,
        )
        self._last = result
        self._results.append(result)
        return result
