# SPDX-License-Identifier: Apache-2.0
"""capsule-emit core — the one-call emit() with anchor-on-by-default.

This is the adoption-surface API described in capsule-emit-quickstart.md.
It wraps ``agent_action_capsule.emit()`` with:
- A friendlier signature (action, operator, developer, agent_input, agent_output, model, verdict, effect)
- Digest-only commitment of agent_input / agent_output (content stays local)
- Async anchor on by default (digest-only; no business content crosses the wire)
- Automatic JSONL ledger append
- A typed EmitResult with .capsule_id and .anchored

The ``confirms`` parameter threads a "did → confirmed" chain without a scheduler.

The same emit() calls and ledger files are compatible with gateway layers that
enforce declared manifests — no code changes required to add enforcement on top.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from agent_action_capsule import emit as _base_emit
from agent_action_capsule.anchor import anchor as _simple_anchor
from agent_action_capsule.contracts import Disposition, EffectRecord, InvariantError

from .ledger import append_to_ledger

__all__ = ["emit", "EmitResult"]

_DEFAULT_LEDGER = "ledger.jsonl"


def _digest(value: Any) -> str:
    """SHA-256 of the canonical JSON serialization of value."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class EmitResult:
    """The result of a capsule-emit emit() call."""

    capsule_id: str
    anchored: bool
    capsule: dict

    def __repr__(self) -> str:
        return f"EmitResult(capsule_id={self.capsule_id!r}, anchored={self.anchored})"


def emit(
    action: str,
    operator: str = "",
    developer: str = "",
    *,
    runtime: str | None = None,
    agent_input: Any = None,
    agent_output: Any = None,
    model: dict[str, str] | None = None,
    verdict: str = "executed",
    effect: dict[str, Any] | None = None,
    confirms: str | None = None,
    relation: str = "confirms",
    anchor: bool = True,
    ledger: str | os.PathLike = _DEFAULT_LEDGER,
    anchor_url: str | None = None,
    human_disposed: bool = False,
    approver: str = "policy",
    decision: str = "accept",
) -> EmitResult:
    """Emit a sealed, optionally anchored Agent Action Capsule.

    Args:
        action: A short, stable action name (e.g. ``"write_po"``).
        operator: Tenant / org identifier.
        developer: Agent name + version (e.g. ``"po-agent@v1"``).
        runtime: Framework hint (e.g. ``"langchain"``); stored in compute_attestation.
        agent_input: The agent's input (any JSON-serializable value). Digest-committed;
            the raw value never leaves the process.
        agent_output: The agent's output. Digest-committed.
        model: Dict with ``"provider"`` and ``"model_id"`` keys.
        verdict: Disposition verdict_class (e.g. ``"executed"``, ``"confirmed"``).
        effect: Effect dict with ``"type"`` and ``"status"`` (and optional ``"autonomy"``).
        confirms: capsule_id of the prior capsule this one chains to.
        relation: Chain relation (``"confirms"`` | ``"supersedes"`` | ``"escalates"`` | …).
            Raises ``ValueError`` when ``confirms`` is None. Default ``"confirms"``.
        anchor: When True (default), fire-and-forget async digest-only anchor submission.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor_url: Override the anchor endpoint (else reads ``AAC_ANCHOR_URL`` env var).
        human_disposed: Whether a human made the disposition decision. When True,
            ``approver`` MUST be ``"human"`` — raises ``ValueError`` otherwise.
        approver: Who approved the disposition: ``"human"`` or ``"policy"`` (default).
        decision: Disposition decision string (default ``"accept"``).

    Returns:
        :class:`EmitResult` with ``.capsule_id`` and ``.anchored``.
    """
    if human_disposed and approver != "human":
        raise InvariantError(
            "human_disposed=True requires approver='human' — "
            "pass approver='human' or set human_disposed=False"
        )
    if relation != "confirms" and confirms is None:
        raise ValueError(
            f"relation={relation!r} requires confirms=<capsule_id> — "
            "a chain relation needs a chain target"
        )

    compute_att: dict[str, Any] = {}
    if agent_input is not None:
        compute_att["agent_input_digest"] = _digest(agent_input)
    if agent_output is not None:
        compute_att["agent_output_digest"] = _digest(agent_output)
    if runtime is not None:
        compute_att["runtime"] = runtime

    model_id: str | None = None
    provider: str | None = None
    if model:
        model_id = model.get("model_id")
        provider = model.get("provider")
        extra_chip = {k: v for k, v in model.items() if k not in ("model_id", "provider")}
        if extra_chip:
            compute_att.update(extra_chip)

    effect_record: EffectRecord | None = None
    if effect is not None:
        eff_status = effect.get("status", "dispatched")
        response_digest: str | None = None
        if eff_status == "confirmed":
            # §5.2 confirmed-effect invariant: must supply response_digest.
            # Auto-derive from agent_output when available; else from the
            # confirms capsule_id (the "observed response" in a confirm chain).
            if agent_output is not None:
                response_digest = _digest(agent_output)
            elif confirms is not None:
                response_digest = _digest({"confirmed_capsule_id": confirms})
        effect_record = EffectRecord(
            type=effect.get("type", action),
            status=eff_status,
            response_digest=response_digest,
        )

    disposition = Disposition(
        decision=decision,
        approver=approver,
        human_disposed=human_disposed,
        verdict_class=verdict,
    )

    chain_relation: str | None = None
    if confirms is not None:
        chain_relation = relation

    capsule = _base_emit(
        action_id=None,
        action_type="decide" if verdict in ("executed", "confirmed", "denied", "blocked") else "fyi",
        operator=operator,
        developer=developer,
        model_id=model_id,
        provider=provider,
        compute_attestation=compute_att if compute_att else None,
        effect=effect_record,
        prior_capsule_id=confirms,
        chain_relation=chain_relation,
        disposition=disposition,
        tool_name=action,
    )

    append_to_ledger(capsule, ledger)

    anchored = False
    if anchor:
        endpoint = anchor_url or os.environ.get("AAC_ANCHOR_URL", None)
        _simple_anchor(
            capsule["capsule_id"],
            **({"endpoint": endpoint} if endpoint else {}),
        )
        anchored = True

    return EmitResult(
        capsule_id=capsule["capsule_id"],
        anchored=anchored,
        capsule=capsule,
    )
