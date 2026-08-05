# SPDX-License-Identifier: Apache-2.0
"""capsule-emit core — the one-call emit() with anchor-on-by-default.

This is the adoption-surface API described in capsule-emit-quickstart.md.
It wraps ``agent_action_capsule.emit()`` with:
- A friendlier signature (action, operator, developer, agent_input, agent_output, model, verdict, effect)
- Digest-only commitment of agent_input / agent_output (content stays local)
- Async anchor on by default (digest-only; no business content crosses the wire)
- Automatic JSONL ledger append
- A typed EmitResult with .capsule_id, .anchored, and .anchor_status

The ``confirms`` parameter threads a "did → confirmed" chain without a scheduler.

The same emit() calls and ledger files are compatible with gateway layers that
enforce declared manifests — no code changes required to add enforcement on top.

**Attestation honesty (spec §3.2 MUST):** ``anchored`` is reported ONLY when a
real ``AnchorResult`` confirms the submission — never merely because anchoring
was requested. The default (non-blocking) anchor path can only ever report the
weaker ``anchor_status="submitted"``; pass ``anchor_wait=<seconds>`` to block
for a real confirmed/failed outcome. See ``transparent.py`` / ``verify.py`` /
``cli.py`` in ``agent_action_capsule`` for the same rule enforced elsewhere.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from agent_action_capsule import emit as _base_emit
from agent_action_capsule.anchor import AnchorError, AnchorFuture, AnchorResult, async_anchor
from agent_action_capsule.canonical import json_digest
from agent_action_capsule.contracts import Disposition, EffectRecord, InvariantError

from .ledger import append_to_ledger

__all__ = ["emit", "EmitResult"]

_DEFAULT_LEDGER = "ledger.jsonl"

AnchorStatus = Literal["confirmed", "submitted", "failed", "skipped"]

#: How long the atexit handler blocks, in total, joining outstanding anchor
#: futures before giving up and warning. Overridable for tests.
_ATEXIT_ANCHOR_TIMEOUT = float(
    os.environ.get("CAPSULE_EMIT_ATEXIT_ANCHOR_TIMEOUT", "5.0")
)

_pending_anchors_lock = threading.Lock()
# capsule_id -> (AnchorFuture, endpoint-or-None)
_pending_anchors: dict[str, tuple[AnchorFuture, str | None]] = {}


def _track_pending_anchor(capsule_id: str, future: AnchorFuture, endpoint: str | None) -> None:
    with _pending_anchors_lock:
        _pending_anchors[capsule_id] = (future, endpoint)


def _untrack_pending_anchor(capsule_id: str) -> None:
    with _pending_anchors_lock:
        _pending_anchors.pop(capsule_id, None)


def _join_pending_anchors_at_exit() -> None:
    """Join outstanding anchor futures with a bounded shared timeout.

    Never a silent-success path: futures that time out or resolve to an
    ``AnchorError`` get a ``RuntimeWarning`` naming the capsule_id and the
    endpoint. Futures that resolve to a real ``AnchorResult`` are silently
    dropped — that is a genuine success, not a claim we have to hedge.
    """
    with _pending_anchors_lock:
        pending = list(_pending_anchors.items())
    if not pending:
        return
    deadline = time.monotonic() + _ATEXIT_ANCHOR_TIMEOUT
    for capsule_id, (future, endpoint) in pending:
        remaining = max(0.0, deadline - time.monotonic())
        result = future.result(timeout=remaining)
        if result is None:
            display_endpoint = endpoint or "the default anchor endpoint (AAC_ANCHOR_URL unset)"
            warnings.warn(
                f"capsule-emit: anchor submission for capsule_id={capsule_id!r} to "
                f"{display_endpoint} did not complete before interpreter shutdown "
                f"(joined for {_ATEXIT_ANCHOR_TIMEOUT}s) — outcome unknown, "
                "the transparency log may not contain this capsule.",
                RuntimeWarning,
                stacklevel=1,
            )
        elif isinstance(result, AnchorError):
            warnings.warn(
                f"capsule-emit: anchor submission for capsule_id={capsule_id!r} to "
                f"{result.ts_url} failed: {result.error}",
                RuntimeWarning,
                stacklevel=1,
            )
        _untrack_pending_anchor(capsule_id)


atexit.register(_join_pending_anchors_at_exit)


def _digest(value: Any) -> str:
    """SHA-256 over the RFC 8785 (JCS) canonicalization of ``value``.

    Uses the same ``json_digest`` (JCS) as :func:`verify_input_digest`, so a
    faithfully-sealed ``agent_input`` / ``agent_output`` re-verifies.

    Before 0.3.2 this used ``json.dumps(sort_keys=True)``, which diverged from
    the JCS verifier for any value containing a null, an empty container, or a
    non-ASCII field — a faithfully-sealed such receipt then failed
    :func:`verify_input_digest` (returned ``False``). JCS now closes that gap.

    Floats fail closed: a raw JSON float is a §5.1 error (it cannot be
    reproducibly digested), so ``emit()`` raises ``FloatInDigestError`` here
    rather than silently sealing an ``agent_input`` / ``agent_output`` that
    :func:`verify_input_digest` could never confirm. Encode monetary/quantity
    values as exact decimal strings before sealing.

    Non-JSON-native types the legacy encoder tolerated via ``default=str``
    (e.g. tuples, arbitrary objects) still fall back to the legacy sorted-key
    encoding, so those emitters do not break at seal time.
    """
    try:
        return json_digest(value)
    except TypeError:
        # Non-JSON-native types (e.g. tuples, arbitrary objects) — tolerated as
        # before. FloatInDigestError is intentionally NOT caught: a raw float is
        # a spec-defined error, so emit() fails closed at the door.
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class EmitResult:
    """The result of a capsule-emit emit() call.

    ``anchored`` is ``True`` ONLY when a real ``AnchorResult`` confirmed the
    submission (i.e. ``anchor_wait`` was set and the future resolved to a
    success before the wait elapsed). The default non-blocking anchor path
    never sets ``anchored=True`` — it cannot know the outcome yet. Use
    ``anchor_status`` for the weaker "was it submitted" fact:

    - ``"confirmed"`` — a real ``AnchorResult`` was obtained (``anchored`` is True).
    - ``"submitted"`` — the anchor POST was dispatched but the outcome is not
      yet known (the default; the background thread is still in flight, or
      ``anchor_wait`` timed out before a result arrived).
    - ``"failed"`` — a real ``AnchorError`` was obtained (only observable when
      ``anchor_wait`` is set; otherwise a background failure surfaces only via
      the ``atexit`` warning at interpreter shutdown).
    - ``"skipped"`` — ``anchor=False`` was passed; no submission was attempted.
    """

    capsule_id: str
    anchored: bool
    capsule: dict
    anchor_status: AnchorStatus

    def __repr__(self) -> str:
        return (
            f"EmitResult(capsule_id={self.capsule_id!r}, anchored={self.anchored}, "
            f"anchor_status={self.anchor_status!r})"
        )


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
    anchor_wait: float | None = None,
    human_disposed: bool = False,
    approver: str = "policy",
    decision: str = "accept",
    action_type: str | None = None,
    extra_compute: dict[str, Any] | None = None,
    disposition_authority: str | None = None,
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
        anchor: When True (default), dispatch an async, digest-only SCITT anchor
            submission (:func:`agent_action_capsule.anchor.async_anchor`). Non-blocking
            by default — see ``anchor_wait`` to block for a confirmed outcome, and
            ``EmitResult.anchor_status`` for the non-blocking outcome signal.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor_url: Override the anchor endpoint (else reads ``AAC_ANCHOR_URL`` env var).
        anchor_wait: When set, block up to this many seconds for the anchor
            submission to resolve, and report the real outcome via
            ``EmitResult.anchored`` / ``.anchor_status``. When ``None`` (default),
            ``emit()`` never blocks and ``anchored`` is always ``False`` (the
            submission's outcome is not yet known at return time).
        human_disposed: Whether a human made the disposition decision. When True,
            ``approver`` MUST be ``"human"`` — raises ``ValueError`` otherwise.
        approver: Who approved the disposition: ``"human"`` or ``"policy"`` (default).
        decision: Disposition decision string (default ``"accept"``).
        action_type: ``"decide"`` | ``"act"`` | ``"retrieve"`` | ``"fyi"`` override.
            When ``None`` (default), auto-derived from *verdict* — disposition verbs
            (``"executed"``, ``"confirmed"``, ``"denied"``, ``"blocked"``) map to
            ``"decide"``; anything else maps to ``"fyi"``.
        extra_compute: Extra key/value pairs merged into ``compute_attestation``.
            Use for framework-specific context (MCP request ID, host info, etc.).
        disposition_authority: Opaque grant reference stored in
            ``disposition.authority`` (e.g. an AAuth JTI). Never the token body —
            only the stable identifier that lets a verifier confirm the authorization
            out-of-band.

    Returns:
        :class:`EmitResult` with ``.capsule_id``, ``.anchored``, and ``.anchor_status``.
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
    if extra_compute:
        compute_att.update(extra_compute)

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
        authority=disposition_authority,
    )

    chain_relation: str | None = None
    if confirms is not None:
        chain_relation = relation

    _action_type = action_type if action_type is not None else (
        "decide" if verdict in ("executed", "confirmed", "denied", "blocked") else "fyi"
    )
    capsule = _base_emit(
        action_id=None,
        action_type=_action_type,
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

    capsule_id = capsule["capsule_id"]
    anchored = False
    anchor_status: AnchorStatus
    if not anchor:
        anchor_status = "skipped"
    else:
        endpoint = anchor_url or os.environ.get("AAC_ANCHOR_URL", None)
        future = async_anchor(capsule_id, ts_url=endpoint)
        _track_pending_anchor(capsule_id, future, endpoint)
        if anchor_wait is None:
            anchor_status = "submitted"
        else:
            result = future.result(timeout=anchor_wait)
            if result is None:
                anchor_status = "submitted"
            else:
                _untrack_pending_anchor(capsule_id)
                if isinstance(result, AnchorResult):
                    anchored = True
                    anchor_status = "confirmed"
                else:
                    anchor_status = "failed"

    return EmitResult(
        capsule_id=capsule_id,
        anchored=anchored,
        capsule=capsule,
        anchor_status=anchor_status,
    )
