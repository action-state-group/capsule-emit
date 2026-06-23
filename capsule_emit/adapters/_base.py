# SPDX-License-Identifier: Apache-2.0
"""Shared base for all capsule-emit framework adapters.

All framework adapters (LangChain, CrewAI, Hermes, MCP) extend this base.
It holds operator/developer/ledger config and exposes a single
``emit_capsule()`` helper that calls the top-level ``capsule_emit.emit()``.
"""
from __future__ import annotations

import os
from typing import Any

from capsule_emit.core import EmitResult, emit

__all__ = ["CapsuleEmitterBase"]


class CapsuleEmitterBase:
    """Shared config carrier for capsule-emit framework adapters.

    Args:
        operator: Tenant/org identifier stamped on every capsule.
        developer: Agent name + version.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor: Fire-and-forget anchor on every emit (default: True).
        anchor_url: Override anchor endpoint (else ``AAC_ANCHOR_URL`` env var).
        model: Default ``{"provider": ..., "model_id": ...}`` applied to every capsule
            when the adapter cannot auto-capture the model from the framework. Can be
            overridden per-emit by passing ``model=`` to :meth:`emit_capsule`.
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        ledger: str | os.PathLike = "ledger.jsonl",
        anchor: bool = True,
        anchor_url: str | None = None,
        model: dict[str, str] | None = None,
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._anchor_url = anchor_url
        self._default_model = model
        self._last: EmitResult | None = None
        self._results: list[EmitResult] = []

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
        relation: str = "confirms",
    ) -> EmitResult:
        """Emit one capsule for a completed tool call.

        ``model`` falls back to the instance-level ``_default_model`` when not
        supplied, which itself is set by ``model=`` in the constructor or overridden
        per-call by framework adapters that auto-capture the model (e.g. LangChain).
        """
        result = emit(
            action=action,
            operator=self._operator,
            developer=self._developer,
            agent_input=tool_input,
            agent_output=tool_output,
            verdict=verdict,
            effect=effect,
            confirms=prior_capsule_id,
            relation=relation,
            anchor=self._anchor,
            ledger=self._ledger,
            anchor_url=self._anchor_url,
            model=model if model is not None else self._default_model,
            human_disposed=human_disposed,
            approver=approver,
            decision=decision,
        )
        self._last = result
        self._results.append(result)
        return result
