# SPDX-License-Identifier: Apache-2.0
"""capsule-emit core — the one-call emit() with anchor-on-by-default.

This is the adoption-surface API described in capsule-emit-quickstart.md.
It wraps ``agent_action_capsule.emit()`` with:
- A friendlier signature (action, operator, developer, agent_input, agent_output, model, verdict, effect)
- Digest-only commitment of agent_input / agent_output (content stays local)
- Per-emit random salt on digest fields by default (prevents cross-capsule correlation)
- Async anchor on by default (digest-only; no business content crosses the wire)
- Automatic JSONL ledger append
- A typed EmitResult with .capsule_id, .anchored, .receipt and .wait_receipt()

The ``confirms`` parameter threads a "did → confirmed" chain without a scheduler.

The same emit() calls and ledger files are compatible with gateway layers that
enforce declared manifests — no code changes required to add enforcement on top.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen

from agent_action_capsule import emit as _base_emit
from agent_action_capsule.anchor import DEFAULT_ANCHOR_ENDPOINT
from agent_action_capsule.contracts import Disposition, EffectRecord, InvariantError

from .ledger import append_to_ledger

__all__ = ["emit", "EmitResult"]

_DEFAULT_LEDGER = "ledger.jsonl"
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Digest helper
# ---------------------------------------------------------------------------


def _digest(value: Any, salt: str | None = None) -> str:
    """SHA-256 of the canonical JSON serialization of value, with optional salt.

    When *salt* is provided, the digest is ``SHA256(salt + "|" + json(value))``.
    This prevents cross-capsule correlation: two capsules with the same logical
    input produce different digests when their salts differ.  The salt is stored
    in ``compute_attestation["digest_salt"]`` so the emitting operator can
    always recompute and verify their own capsules.
    """
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if salt:
        raw = salt + "|" + raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Async anchor with receipt capture + failOpen warning
# ---------------------------------------------------------------------------


@dataclass
class _AnchorFuture:
    """Internal: captures the anchor HTTP response asynchronously."""
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    receipt: dict | None = None
    error: Exception | None = None

    def wait(self, timeout: float = 10.0) -> dict | None:
        """Block until the anchor response arrives (or *timeout* seconds elapse)."""
        self._event.wait(timeout=timeout)
        return self.receipt


def _anchor_async(capsule_id: str, endpoint: str) -> _AnchorFuture:
    """Fire an async anchor POST; capture receipt; log WARNING on failure (failOpen).

    Returns immediately.  The HTTP POST runs in a daemon thread.  When the thread
    finishes, it sets ``future.receipt`` (success) or logs a WARNING and sets
    ``future.error`` (failure).  The capsule is already sealed locally regardless
    of the anchor outcome — the anchor failure is **not** an exception (failOpen).

    To block until the receipt arrives: ``future.wait(timeout=10)``.
    """
    future = _AnchorFuture()
    body = json.dumps({"capsule_id": capsule_id}, separators=(",", ":")).encode()

    def _post() -> None:
        try:
            req = Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10.0) as resp:
                raw = resp.read()
                try:
                    future.receipt = json.loads(raw) if raw else {}
                except Exception:
                    future.receipt = {}
        except Exception as exc:
            future.error = exc
            _log.warning(
                "capsule-emit: anchor submission FAILED for capsule %.16s… — "
                "capsule is sealed locally but NOT committed to the transparency "
                "log (failOpen: the action continues). Error: %s",
                capsule_id,
                exc,
            )
        finally:
            future._event.set()

    threading.Thread(target=_post, daemon=True, name="anchor-post").start()
    return future


# ---------------------------------------------------------------------------
# EmitResult
# ---------------------------------------------------------------------------


@dataclass
class EmitResult:
    """The result of a capsule-emit emit() call.

    Attributes:
        capsule_id: 64-character hex SHA-256 content address of the capsule.
        anchored: ``True`` when an anchor submission was *started* (``anchor=True``
            was passed).  Does **not** mean the submission succeeded — watch
            ``logging.WARNING`` for ``"anchor submission FAILED"`` messages, or
            call :meth:`wait_receipt` to confirm.
        capsule: The full capsule dict (plain JSON; storable / shareable).
        receipt: The anchor's inclusion response dict, once available.  ``None``
            until :meth:`wait_receipt` resolves it, or when ``anchor=False``.
    """

    capsule_id: str
    anchored: bool
    capsule: dict
    receipt: dict | None = None

    def __post_init__(self) -> None:
        # Not a dataclass field — set after construction by emit()
        self._anchor_future: _AnchorFuture | None = None

    def wait_receipt(self, timeout: float = 10.0) -> dict | None:
        """Block until the anchor receipt arrives (up to *timeout* seconds).

        Returns the receipt dict on success, ``None`` on timeout or when
        ``anchor=False`` was passed to :func:`emit`.  The result is also
        stored on :attr:`receipt`.
        """
        if self._anchor_future is not None:
            result = self._anchor_future.wait(timeout=timeout)
            if result is not None:
                self.receipt = result
        return self.receipt

    def __repr__(self) -> str:
        return f"EmitResult(capsule_id={self.capsule_id!r}, anchored={self.anchored})"


# ---------------------------------------------------------------------------
# emit()
# ---------------------------------------------------------------------------


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
    action_type: str | None = None,
    extra_compute: dict[str, Any] | None = None,
    salt_digests: bool = True,
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
            Failures are logged as ``WARNING`` (failOpen) — the action always continues.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor_url: Override the anchor endpoint (else reads ``AAC_ANCHOR_URL`` env var).
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
        salt_digests: When ``True`` (default), prepend a random 16-byte hex salt to
            each input/output before hashing, stored as ``digest_salt`` in
            ``compute_attestation``.  This prevents cross-capsule correlation of
            low-entropy inputs (an adversary cannot build a single rainbow table
            that works across capsules).  Pass ``False`` only when you need
            deterministic digests for testing or cross-call comparison.

    Returns:
        :class:`EmitResult` with ``.capsule_id``, ``.anchored``, ``.receipt``,
        and ``.wait_receipt()``.

    failOpen behaviour:
        When ``anchor=True`` and the transparency log is unreachable, the capsule
        is still sealed and written to the ledger.  A ``WARNING`` is emitted via
        Python's ``logging`` module so the silent outage is never invisible.
        To assert on anchor health, call ``cap.wait_receipt(timeout=N)`` and check
        whether the returned dict is non-None.
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

    # Per-emit random salt for digest privacy (prevents cross-capsule correlation).
    emit_salt: str | None = secrets.token_hex(16) if salt_digests else None

    compute_att: dict[str, Any] = {}
    _had_digest = False
    if agent_input is not None:
        compute_att["agent_input_digest"] = _digest(agent_input, salt=emit_salt)
        _had_digest = True
    if agent_output is not None:
        compute_att["agent_output_digest"] = _digest(agent_output, salt=emit_salt)
        _had_digest = True
    # Only store the salt when there is at least one digest to which it was applied.
    if emit_salt is not None and _had_digest:
        compute_att["digest_salt"] = emit_salt
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
            # Use the salted digest here too so it matches what's stored.
            if agent_output is not None:
                response_digest = _digest(agent_output, salt=emit_salt)
            elif confirms is not None:
                response_digest = _digest({"confirmed_capsule_id": confirms}, salt=emit_salt)
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

    anchored = False
    _future: _AnchorFuture | None = None
    if anchor:
        endpoint = anchor_url or os.environ.get("AAC_ANCHOR_URL") or DEFAULT_ANCHOR_ENDPOINT
        _future = _anchor_async(capsule["capsule_id"], endpoint)
        anchored = True

    result = EmitResult(
        capsule_id=capsule["capsule_id"],
        anchored=anchored,
        capsule=capsule,
    )
    result._anchor_future = _future
    return result
