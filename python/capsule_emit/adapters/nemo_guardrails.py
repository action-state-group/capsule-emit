# SPDX-License-Identifier: Apache-2.0
"""NVIDIA NeMo Guardrails recorder — one capsule per rail decision, chained per turn.

    from nemoguardrails import LLMRails, RailsConfig
    from capsule_emit.adapters.nemo_guardrails import NeMoGuardrailsCapsuleRecorder

    recorder = NeMoGuardrailsCapsuleRecorder(operator="acme-co", developer="support-bot@v1")
    rails = LLMRails(RailsConfig.from_path("./config"))
    recorder.attach(rails)

Requires ``pip install "capsule-emit[nemo-guardrails]"``.

Sibling of the listener family (``crewai_listener`` / ``langchain_listener`` /
``agno_listener`` / ``strands_listener``), with the same core/shell split, the same
base funnel (everything goes out through :meth:`CapsuleEmitterBase.emit_capsule`),
the same digest-only privacy rule, and the same never-raises boundary.

What it records
---------------
Not tool calls — **rail decisions**. NeMo Guardrails' stable ``LLMRails`` engine
reports, per turn, a list of ``ActivatedRail`` objects
(``nemoguardrails/rails/llm/options.py:234``) carrying ``type`` (input / output /
dialog / generation / retrieval), ``name`` (the flow that implemented the rail),
``decisions``, ``stop``, ``executed_actions`` and timings. Newer rails additionally
return a typed ``RailOutcome`` (``nemoguardrails/actions/rail_outcome.py:80``) whose
``decision`` is one of ``RailDecision.ALLOW`` / ``BLOCK`` / ``TRANSFORM``
(``:50-55``) — in their own words, *"the three mutually exclusive things a rail can
decide."* That enum is the spine of this adapter.

Each turn seals as a **chain**:

- head    → ``action="nemo_guardrails_turn"``, ``effect.status="planned"``, the
  turn's input digest — the commitment record
- per rail → one capsule, ``confirms``-chained to the one before it
- tail    → ``action="nemo_guardrails_turn"``, ``effect.status="confirmed"``, the
  final response digest, and the turn's aggregate verdict

So the ledger answers "which rails ran, in what order, and what did each decide"
for a given input digest — the evidence layer under the guardrail, not the
guardrail.

Decision → capsule vocabulary
------------------------------
- ``ALLOW``     → ``verdict="executed"``, ``effect.status="confirmed"``
- ``BLOCK``     → ``verdict="blocked"``, ``effect.status="planned"``
- ``TRANSFORM`` → ``verdict="executed"``, ``effect.status="confirmed"``, with the
  rewritten text digest-committed per target

``effect.status`` stays inside the reserved set
``planned|dispatched|confirmed|failed|reverted`` (§5.2): an unrecognised status
derives ``effect_mode="dispatched_unconfirmed"``, which would claim the guarded
content passed when a BLOCK means it did not. ``planned`` is the spec's carve for
exactly that, and it matches what ``capsule_emit.gate`` already emits on a block
(``gate.py:236-243``).

``TRANSFORM`` gets no verdict of its own here. The existing ``verdict_class``
vocabulary in this repo is ``executed`` / ``errored`` / ``blocked`` / ``denied`` /
``confirmed`` / ``countersign_refused``; minting an ``altered`` seventh value is a
repo-wide vocabulary call, not an adapter-local one. Until that call is made, a
transform is an ``executed`` rail whose ``nemo_transform_targets`` and rewritten-text
digests carry the alteration. **Flagged for the desk, not decided here.**

A rail that raised instead of deciding is reported by the engine as
``RailOutcome.failure()`` — a ``BLOCK`` with ``failed=True`` (``:148-160``). That is
sealed as ``verdict="errored"`` / ``effect.status="failed"``, because a rail that
crashed and a rail that deliberately refused are different facts and the ledger
should not flatten them.

Observation only, and structurally so
--------------------------------------
This recorder never decides anything. The two turn-level wirings both run *after*
the engine has already applied the rails, and neither can alter the response:
``Tracer.export_async`` is invoked at ``llmrails.py:1314``, past every rail. The
in-flow action (:meth:`NeMoRailsCore.seal_rail_decision`) returns a dict marked
``observation_only`` and its return value is never a gate input — flows must not
branch on it. Deny belongs to the gate layer; see the ``observation_mode`` proposal
in capsule-emit #48.

Never-raises is load-bearing on the tracing path
-------------------------------------------------
``Tracer.export_async`` fans adapters out with ``asyncio.gather(*tasks)`` and **no**
``return_exceptions=True`` (``nemoguardrails/tracing/tracer.py``), and the call site
``await tracer.export_async()`` (``llmrails.py:1314``) is **not** wrapped in a
``try``. A tracing adapter that raises therefore propagates straight out of the
user's ``generate_async`` — a broken evidence layer would turn a working, correctly
guarded turn into a failed request. Every sealing path here is individually guarded;
a broken ledger or anchor endpoint warns and is skipped, and can never affect the
guarded application.

Floats fail closed, and this log is full of them
-------------------------------------------------
``ActivatedRail`` carries ``duration``, ``started_at`` and ``finished_at`` as raw
floats, and ``ExecutedAction`` repeats all three. A raw float in a digest-bearing
field raises ``FloatInDigestError`` (CPB wire rule: non-integer quantities travel as
JSON strings). Handing an ``ActivatedRail`` to the digest layer unprojected means
*every* capsule fails closed and the adapter silently seals nothing at all — the
failure mode looks exactly like "no rails ran". Everything this module digests is
therefore routed through :func:`_scrub`, which converts floats with
``capsule_emit.numbers.float_to_str`` (RFC 8785 §3.2.2.3). Covered by
``test_rail_timings_are_float_scrubbed_not_dropped``.

Privacy: inputs, outputs, rail reasons and transform texts are digested, never
stored — the ledger carries ``agent_input_digest`` / ``agent_output_digest`` only.
``ExecutedAction.action_params`` routinely holds the raw user message, which is
precisely why it goes through the digest funnel rather than into a metadata field.

Engine scope
-------------
Stable ``LLMRails`` only. The ``IORails`` tool-calling engine is marked opt-in and
experimental in NVIDIA's own docs, so nothing here targets it; ``RailOutcome`` is
documented as engine-neutral (``rail_outcome.py:81-86``), so the core mapping should
carry over if and when that engine settles, but that is untested and unclaimed.

Verified against the released ``nemoguardrails==0.24.0`` wheel (the extra pins it);
every line number above was read from that wheel, not from a clone and not from docs.

All sealing logic lives in the framework-free :class:`NeMoRailsCore` (fully testable
without nemoguardrails installed); :class:`NeMoGuardrailsCapsuleRecorder` is the thin
shell that binds it to a live ``LLMRails`` via ``register_action`` /
``register_action_param`` (``llmrails.py:1735`` / ``:1740``).
"""
from __future__ import annotations

import warnings
from typing import Any

from ..numbers import float_to_str
from ._base import CapsuleEmitterBase

__all__ = [
    "ACTION_PARAM_NAME",
    "LOG_ADAPTER_NAME",
    "NeMoGuardrailsCapsuleRecorder",
    "NeMoRailsCore",
    "RAIL_ACTION_NAME",
    "get_capsule_log_adapter_class",
    "register_capsule_log_adapter",
]

#: Name the in-flow sealing action is registered under (``execute`` it from Colang).
RAIL_ACTION_NAME = "capsule_emit_seal_rail_decision"

#: Name the recorder is registered under as an action param, so any custom action
#: can seal by declaring a ``capsule_recorder`` keyword argument.
ACTION_PARAM_NAME = "capsule_recorder"

#: Name the tracing log adapter is registered under, for ``tracing.adapters`` in YAML.
LOG_ADAPTER_NAME = "CapsuleEmit"

#: The turn envelope's action name (head and tail of every per-turn chain).
TURN_ACTION = "nemo_guardrails_turn"

# RailDecision values (nemoguardrails/actions/rail_outcome.py:50-55). Mirrored as
# plain strings so this module stays importable without nemoguardrails installed;
# RailDecision is a `str, Enum`, so equality against these literals holds.
DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"
DECISION_TRANSFORM = "transform"
#: Not one of theirs: what we record when a rail ran but exposed no typed outcome.
DECISION_UNKNOWN = "unknown"

# Rail types the engine reports (options.py:236). Used only for filtering.
RAIL_TYPES = ("input", "output", "dialog", "generation", "retrieval")


def _scrub(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace raw floats with their canonical decimal strings.

    The CPB wire rule rejects floating-point tokens in digest-bearing fields
    (``FloatInDigestError``); non-integer quantities travel as JSON strings. The
    NeMo rail log is float-dense — ``duration``/``started_at``/``finished_at`` on
    every ``ActivatedRail`` and every ``ExecutedAction`` — so without this
    projection the adapter would fail closed on essentially every capsule and seal
    nothing, which is indistinguishable from "no rails ran".

    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass and
    must survive as a JSON boolean. Unknown objects are stringified rather than
    guessed at; depth is bounded so a self-referential metadata dict cannot spin.
    """
    if _depth > 12:
        return "<omitted:depth>"
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float):
        try:
            return float_to_str(value)
        except Exception:  # noqa: BLE001 — NaN/Inf name the field; never worth raising here
            return "<omitted:nonfinite>"
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): _scrub(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub(v, _depth=_depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<omitted:{len(value)} bytes>"
    # Pydantic models (ActivatedRail, ExecutedAction) and dataclasses (RailOutcome,
    # TransformSpec) are duck-typed here so this module needs no nemoguardrails import.
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        try:
            return _scrub(dumped(mode="python"), _depth=_depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__") and vars(value):
        return _scrub(dict(vars(value)), _depth=_depth + 1)
    slots = getattr(type(value), "__slots__", None)
    if slots:
        return _scrub({s: getattr(value, s, None) for s in slots}, _depth=_depth + 1)
    return str(value)


def _outcome_of(action: Any) -> Any:
    """The ``RailOutcome`` an executed action returned, or ``None``.

    Duck-typed on the two fields that define the type (``rail_outcome.py:88-92``)
    rather than on ``isinstance``, so the core stays importable without the SDK and
    so a rail returning a compatible object from elsewhere is still understood.
    """
    ret = getattr(action, "return_value", None)
    if ret is None:
        return None
    if hasattr(ret, "decision") and hasattr(ret, "transforms"):
        return ret
    return None


class NeMoRailsCore(CapsuleEmitterBase):
    """Framework-free core of the NeMo Guardrails recorder.

    Every public method takes duck-typed objects (anything with the
    ``ActivatedRail`` field names), so the whole behavior is exercised in tests
    without nemoguardrails installed. :class:`NeMoGuardrailsCapsuleRecorder` is the
    shell that binds it to a live ``LLMRails``.

    Args:
        record_allow: Seal a capsule for rails that decided ALLOW (default True).
            Leaving this on is the point: an allow that was never recorded is
            indistinguishable from a rail that never ran, and "absent is never
            pass". Turn it off only when volume genuinely forces it, and know that
            the ledger then attests refusals only.
        rail_types: Restrict recording to these rail types (default: all of
            :data:`RAIL_TYPES`).
        include_turn_envelope: Seal the head/tail ``nemo_guardrails_turn`` capsules
            that open and close each turn's chain (default True). With it off,
            per-rail capsules still chain to each other but the turn has no
            commitment record and no aggregate verdict.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer, ledger,
            anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        record_allow: bool = True,
        rail_types: tuple[str, ...] | None = None,
        include_turn_envelope: bool = True,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._record_allow = record_allow
        self._rail_types = tuple(rail_types) if rail_types is not None else RAIL_TYPES
        self._include_turn_envelope = include_turn_envelope

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising.

        Load-bearing: on the tracing path a raised exception propagates out of the
        caller's ``generate_async`` (see the module docstring).
        """
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: NeMo Guardrails recorder failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    @staticmethod
    def classify(rail: Any) -> tuple[str, bool]:
        """``(decision, failed)`` for one activated rail.

        Prefers the typed ``RailOutcome`` returned by an executed action — that is
        the rail's own statement of what it decided. Falls back to the engine's
        coarser signals only when no action returned one, which is the case for
        older community rails and for dialog rails that never call a check action:

        - ``rail.stop`` is True → the rail halted further processing → BLOCK
        - otherwise → ALLOW when the rail ran to completion

        A rail we cannot classify at all is recorded as :data:`DECISION_UNKNOWN`
        rather than guessed into ALLOW. Reporting "could not determine" is honest;
        reporting a pass we did not observe is not.
        """
        actions = getattr(rail, "executed_actions", None) or []
        for action in actions:
            outcome = _outcome_of(action)
            if outcome is None:
                continue
            decision = getattr(outcome, "decision", None)
            # RailDecision is a `str, Enum`, so `.value` and the member both compare
            # equal to the literal; normalize either way.
            text = getattr(decision, "value", decision)
            failed = bool(getattr(outcome, "failed", False))
            if isinstance(text, str) and text in (
                DECISION_ALLOW,
                DECISION_BLOCK,
                DECISION_TRANSFORM,
            ):
                return text, failed
            return DECISION_UNKNOWN, failed
        stop = getattr(rail, "stop", None)
        if stop is True:
            return DECISION_BLOCK, False
        if stop is False:
            return DECISION_ALLOW, False
        return DECISION_UNKNOWN, False

    @staticmethod
    def _verdict_and_status(decision: str, failed: bool) -> tuple[str, str]:
        """Map a rail decision onto ``(verdict_class, effect.status)``.

        See the module docstring for why TRANSFORM does not mint a new verdict and
        why a failed rail is ``errored``/``failed`` rather than ``blocked``.
        """
        if failed:
            return "errored", "failed"
        if decision == DECISION_BLOCK:
            return "blocked", "planned"
        if decision in (DECISION_ALLOW, DECISION_TRANSFORM):
            return "executed", "confirmed"
        return "executed", "planned"

    def _rail_request(self, rail: Any) -> dict[str, Any]:
        """The digest-committed *request* side of one rail capsule.

        What the rail was asked to judge: its identity plus the actions it ran and
        the params they were called with. ``ExecutedAction.action_params`` routinely
        holds the raw user message, which is exactly why it goes through the digest
        funnel rather than into a readable metadata field.
        """
        actions = getattr(rail, "executed_actions", None) or []
        return {
            "rail_type": str(getattr(rail, "type", "") or "unknown"),
            "rail_name": str(getattr(rail, "name", "") or "unknown"),
            "executed_actions": _scrub(
                [
                    {
                        "action_name": str(getattr(a, "action_name", "") or "unknown"),
                        "action_params": getattr(a, "action_params", None) or {},
                        "duration": getattr(a, "duration", None),
                    }
                    for a in actions
                ]
            ),
            "started_at": _scrub(getattr(rail, "started_at", None)),
        }

    def _rail_outcome_body(self, rail: Any, decision: str, failed: bool) -> dict[str, Any]:
        """The digest-committed *response* side: what the rail actually decided.

        A rail's decision is the observed response of the rail check, so this is
        what satisfies the §5.2 confirmed-effect invariant (``effect.status
        "confirmed"`` REQUIRES a 64-hex ``response_digest`` over an observed
        response — ``agent_action_capsule/contracts.py:200-206``). Digesting the
        decision rather than inventing a placeholder is what makes ``confirmed``
        an honest status here.
        """
        actions = getattr(rail, "executed_actions", None) or []
        outcome = next((o for o in (_outcome_of(a) for a in actions) if o is not None), None)
        body: dict[str, Any] = {
            "decision": decision,
            "failed": failed,
            "stop": bool(getattr(rail, "stop", False)),
            "engine_decisions": _scrub(list(getattr(rail, "decisions", None) or [])),
            "return_values": _scrub([getattr(a, "return_value", None) for a in actions]),
            "duration": _scrub(getattr(rail, "duration", None)),
            "finished_at": _scrub(getattr(rail, "finished_at", None)),
        }
        if outcome is not None:
            body["reason"] = getattr(outcome, "reason", None)
            body["metadata"] = _scrub(getattr(outcome, "metadata", None) or {})
            transforms = getattr(outcome, "transforms", None) or ()
            if transforms:
                # TransformSpec is (target, text) — rail_outcome.py:66-76. The
                # rewritten text is digest-committed, so the alteration is provable
                # without the text ever entering the ledger.
                body["transforms"] = [
                    {
                        "target": str(getattr(getattr(t, "target", None), "value", getattr(t, "target", ""))),
                        "text": getattr(t, "text", None),
                    }
                    for t in transforms
                ]
        return body

    # -- recording ---------------------------------------------------------

    def record_rail(
        self,
        rail: Any,
        *,
        prior_capsule_id: str | None = None,
        interaction_id: str | None = None,
    ) -> Any | None:
        """Seal one capsule for one activated rail. Returns the EmitResult, or None.

        None means either "filtered out" (rail type not selected, or an ALLOW while
        ``record_allow`` is off) or "sealing failed and warned" — callers chain on
        the last id that actually sealed, so a single failure cannot orphan the rest
        of the chain.
        """
        try:
            rail_type = str(getattr(rail, "type", "") or "unknown")
            if rail_type not in self._rail_types:
                return None
            decision, failed = self.classify(rail)
            request_body = self._rail_request(rail)
            outcome_body = self._rail_outcome_body(rail, decision, failed)
            rail_name = str(getattr(rail, "name", "") or "unknown")
        except Exception as exc:  # noqa: BLE001 — a malformed rail object is not worth a crash
            warnings.warn(
                f"capsule-emit: NeMo Guardrails recorder could not read a rail: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None
        if decision == DECISION_ALLOW and not self._record_allow and not failed:
            return None
        verdict, status = self._verdict_and_status(decision, failed)
        compute: dict[str, Any] = {
            "nemo_rail_type": rail_type,
            "nemo_rail_name": rail_name,
            "nemo_rail_decision": decision,
            "observation_mode": "rail_log",
        }
        if failed:
            compute["nemo_rail_failed"] = True
        if decision == DECISION_TRANSFORM:
            outcome = next(
                (o for o in (_outcome_of(a) for a in getattr(rail, "executed_actions", None) or []) if o),
                None,
            )
            targets = [
                str(getattr(getattr(t, "target", None), "value", getattr(t, "target", "")))
                for t in (getattr(outcome, "transforms", None) or ())
            ]
            if targets:
                compute["nemo_transform_targets"] = targets
        if interaction_id is not None:
            compute["nemo_interaction_id"] = str(interaction_id)
        return self._seal(
            action=f"rail:{rail_type}:{rail_name}",
            tool_input=request_body,
            tool_output=outcome_body,
            verdict=verdict,
            effect={"type": f"guardrail_{rail_type}", "status": status},
            prior_capsule_id=prior_capsule_id,
            action_type="fyi",
            runtime="nemoguardrails",
            extra_compute=compute,
        )

    def _safe_classify(self, rail: Any) -> tuple[str, bool]:
        """classify() that reports UNKNOWN instead of propagating a bad rail object."""
        try:
            return self.classify(rail)
        except Exception:  # noqa: BLE001
            return DECISION_UNKNOWN, False

    def record_turn(
        self,
        activated_rails: Any,
        *,
        interaction_id: str | None = None,
        turn_input: Any = None,
        turn_output: Any = None,
    ) -> list[Any]:
        """Seal a whole turn as one chain. Returns the EmitResults, in chain order.

        Chain shape: head (``planned``) → one capsule per recorded rail → tail
        (``confirmed``). Each link ``confirms`` the previous one that actually
        sealed.
        """
        rails = list(activated_rails or [])
        results: list[Any] = []
        prior: str | None = None

        if self._include_turn_envelope:
            head = self._seal(
                action=TURN_ACTION,
                tool_input=_scrub(turn_input),
                verdict="executed",
                effect={"type": TURN_ACTION, "status": "planned"},
                action_type="fyi",
                runtime="nemoguardrails",
                extra_compute=self._turn_compute(interaction_id, rails, phase="open"),
            )
            if head is not None:
                results.append(head)
                prior = getattr(head, "capsule_id", None)

        for rail in rails:
            result = self.record_rail(rail, prior_capsule_id=prior, interaction_id=interaction_id)
            if result is None:
                continue
            results.append(result)
            prior = getattr(result, "capsule_id", None) or prior

        if self._include_turn_envelope:
            blocked = any(self._safe_classify(r)[0] == DECISION_BLOCK for r in rails)
            failed = any(self._safe_classify(r)[1] for r in rails)
            if failed:
                verdict, status = "errored", "failed"
            elif blocked:
                verdict, status = "blocked", "planned"
            else:
                verdict, status = "executed", "confirmed"
            # The tail is `confirmed` on a clean turn, and §5.2 requires a
            # response_digest over an *observed* response. On the tracing path the
            # engine hands us no response text (InteractionLog carries none), so the
            # observed response is the rail verdict set itself — stated as such
            # rather than padded with a placeholder.
            observed = {
                "response": _scrub(turn_output),
                "rail_decisions": [
                    f"{getattr(r, 'type', '?')}:{getattr(r, 'name', '?')}={self._safe_classify(r)[0]}"
                    for r in rails
                ],
            }
            tail = self._seal(
                action=TURN_ACTION,
                tool_input=_scrub(turn_input),
                tool_output=observed,
                verdict=verdict,
                effect={"type": TURN_ACTION, "status": status},
                prior_capsule_id=prior,
                action_type="fyi",
                runtime="nemoguardrails",
                extra_compute=self._turn_compute(interaction_id, rails, phase="close"),
            )
            if tail is not None:
                results.append(tail)
        return results

    def _turn_compute(self, interaction_id: str | None, rails: list[Any], *, phase: str) -> dict[str, Any]:
        compute: dict[str, Any] = {
            "nemo_turn_phase": phase,
            "nemo_activated_rail_count": len(rails),
            "observation_mode": "rail_log",
        }
        if interaction_id is not None:
            compute["nemo_interaction_id"] = str(interaction_id)
        if phase == "close":
            compute["nemo_rail_decisions"] = [
                f"{getattr(r, 'type', '?')}:{getattr(r, 'name', '?')}={self._safe_classify(r)[0]}" for r in rails
            ]
        return compute

    def record_interaction_log(self, interaction_log: Any) -> list[Any]:
        """Seal a turn from an ``InteractionLog`` (the tracing-adapter path).

        ``InteractionLog`` (``tracing/interaction_types.py:27``) carries ``id`` and
        the same ``activated_rails`` list. It deliberately does not carry the turn's
        input/output text, so the head/tail capsules on this path commit to the rail
        set rather than to content — which is the privacy-preferable default anyway.
        """
        return self.record_turn(
            getattr(interaction_log, "activated_rails", None) or [],
            interaction_id=getattr(interaction_log, "id", None),
        )

    def record_generation_response(self, response: Any, *, turn_input: Any = None) -> list[Any]:
        """Seal a turn from a ``GenerationResponse`` (the caller-driven path).

        Requires the caller to have asked for the log::

            res = await rails.generate_async(
                messages=msgs,
                options=GenerationOptions(log={"activated_rails": True}),
            )
            recorder.record_generation_response(res, turn_input=msgs)

        With no ``options`` the engine returns a bare string and there is nothing to
        record; that is reported as an empty list, never as a clean turn.
        """
        log = getattr(response, "log", None)
        if log is None:
            return []
        return self.record_turn(
            getattr(log, "activated_rails", None) or [],
            turn_input=turn_input,
            turn_output=getattr(response, "response", None),
        )

    # -- the in-flow registered action -------------------------------------

    async def seal_rail_decision(
        self,
        rail: str = "custom",
        decision: Any = None,
        rail_type: str = "custom",
        evidence: Any = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Registered Colang action: seal one decision at the point a rail makes it.

        Registered on a live instance as :data:`RAIL_ACTION_NAME` by
        :meth:`NeMoGuardrailsCapsuleRecorder.attach`::

            define subflow my rail
              $outcome = execute my_check(text=$user_message)
              execute capsule_emit_seal_rail_decision(rail="my rail", decision=$outcome)

        ``decision`` may be a ``RailOutcome``, a plain string, a bool, or None.

        **The return value is not a decision.** It is a dict carrying the sealed
        capsule id and ``observation_only: True``, provided so a flow can chain a
        later capsule to this one. Flows must not branch on it — this action records
        what a rail decided and never participates in deciding it.

        Async because the action dispatcher awaits coroutine actions directly, and
        sealing is a local append, so there is no benefit to a thread hop.
        """
        failed = bool(getattr(decision, "failed", False))
        # A RailOutcome carries the decision one level down (rail_outcome.py:88).
        if hasattr(decision, "decision") and hasattr(decision, "transforms"):
            decision = decision.decision
        text = getattr(decision, "value", decision)
        if isinstance(text, bool):
            text = DECISION_BLOCK if text else DECISION_ALLOW
        elif text is None:
            text = DECISION_UNKNOWN
        else:
            text = str(text).lower()
        if text not in (DECISION_ALLOW, DECISION_BLOCK, DECISION_TRANSFORM):
            text = DECISION_UNKNOWN
        verdict, status = self._verdict_and_status(text, failed)
        result = self._seal(
            action=f"rail:{rail_type}:{rail}",
            tool_input=_scrub({"rail": rail, "rail_type": rail_type, "evidence": evidence}),
            tool_output=_scrub({"decision": text, "failed": failed}),
            verdict=verdict,
            effect={"type": f"guardrail_{rail_type}", "status": status},
            action_type="fyi",
            runtime="nemoguardrails",
            extra_compute={
                "nemo_rail_name": str(rail),
                "nemo_rail_type": str(rail_type),
                "nemo_rail_decision": text,
                "observation_mode": "in_flow_action",
            },
        )
        return {
            "capsule_id": getattr(result, "capsule_id", None),
            "decision": text,
            "observation_only": True,
        }


class NeMoGuardrailsCapsuleRecorder(NeMoRailsCore):
    """Public recorder. Binds :class:`NeMoRailsCore` to a live ``LLMRails``.

    Three wirings, all on the stable ``LLMRails`` engine and all using public
    surfaces only — no fork, no monkeypatch, no private attribute is touched:

    1. **YAML tracing adapter (recommended, zero code).** Call
       :func:`register_capsule_log_adapter` once at import time, then in
       ``config.yml``::

           tracing:
             enabled: true
             adapters:
               - name: CapsuleEmit
                 operator: acme-co
                 developer: support-bot@v1

       ``LogAdapterConfig`` is ``extra="allow"`` (``rails/llm/config.py:240-242``),
       so those keys flow through ``create_log_adapters`` into the adapter's
       constructor. Every turn is recorded with no application change at all.

    2. **Caller-driven.** Pass the response to
       :meth:`NeMoRailsCore.record_generation_response`. Fully explicit; needs
       ``GenerationOptions(log={"activated_rails": True})``.

    3. **In-flow action.** :meth:`attach` registers
       :meth:`NeMoRailsCore.seal_rail_decision` as ``capsule_emit_seal_rail_decision``
       and registers this recorder as the ``capsule_recorder`` action param, so a
       Colang flow (or any custom action declaring that kwarg) can seal at the exact
       point of decision.

    Wirings 1 and 3 compose: the tracing adapter gives the per-turn chain, the action
    gives finer-grained records inside a single rail.
    """

    def attach(self, rails: Any) -> Any:
        """Register the sealing action and this recorder on a live ``LLMRails``.

        Uses ``LLMRails.register_action`` (``llmrails.py:1735``) and
        ``LLMRails.register_action_param`` (``:1740``) — both public, both returning
        ``Self``, neither requiring a subclass or a fork. Returns ``rails`` so the
        call can be chained.

        This registers capability; it does not by itself cause anything to be
        recorded. A Colang flow must ``execute`` the action, or a custom action must
        declare the ``capsule_recorder`` kwarg. For turn-level recording with no
        config change, use wiring 1 or 2.
        """
        rails.register_action(self.seal_rail_decision, RAIL_ACTION_NAME)
        rails.register_action_param(ACTION_PARAM_NAME, self)
        return rails


_LOG_ADAPTER_CLASS: Any = None


def get_capsule_log_adapter_class() -> Any:
    """Build (once) and return the ``InteractionLogAdapter`` subclass.

    Built lazily rather than at module import because subclassing it requires
    nemoguardrails to be installed, and this module must stay importable — and its
    core testable — without the SDK, matching the rest of the adapter family.
    """
    global _LOG_ADAPTER_CLASS
    if _LOG_ADAPTER_CLASS is not None:
        return _LOG_ADAPTER_CLASS

    from nemoguardrails.tracing.adapters.base import InteractionLogAdapter

    class CapsuleEmitLogAdapter(InteractionLogAdapter):
        """Seals one capsule chain per interaction, from the tracing hook.

        Instantiated by ``create_log_adapters`` (``tracing/tracer.py:104-116``) with
        every non-``name`` key from the YAML adapter config, so ``operator`` and
        ``developer`` come straight from ``config.yml``.

        ``transform`` / ``transform_async`` never raise: ``Tracer.export_async``
        gathers adapters without ``return_exceptions`` and its call site
        (``llmrails.py:1314``) is not wrapped, so an exception here would surface as
        a failed generation for the guarded application.
        """

        name = LOG_ADAPTER_NAME

        def __init__(self, **kwargs: Any) -> None:
            operator = kwargs.pop("operator", "")
            developer = kwargs.pop("developer", "")
            recorder_kw = {
                k: kwargs.pop(k)
                for k in (
                    "ledger",
                    "anchor",
                    "anchor_url",
                    "anchor_wait",
                    "model",
                    "max_results",
                    "record_allow",
                    "rail_types",
                    "include_turn_envelope",
                )
                if k in kwargs
            }
            if "rail_types" in recorder_kw and recorder_kw["rail_types"] is not None:
                recorder_kw["rail_types"] = tuple(recorder_kw["rail_types"])
            # Unknown keys are ignored rather than fatal: LogAdapterConfig is
            # extra="allow", so a typo in config.yml must not take down the app.
            self.recorder = NeMoRailsCore(operator=operator, developer=developer, **recorder_kw)

        def transform(self, interaction_log: Any) -> None:
            try:
                self.recorder.record_interaction_log(interaction_log)
            except Exception as exc:  # noqa: BLE001 — never break the guarded turn
                warnings.warn(
                    f"capsule-emit: NeMo Guardrails tracing adapter failed: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

        async def transform_async(self, interaction_log: Any) -> None:
            self.transform(interaction_log)

    _LOG_ADAPTER_CLASS = CapsuleEmitLogAdapter
    return _LOG_ADAPTER_CLASS


def register_capsule_log_adapter(name: str = LOG_ADAPTER_NAME) -> Any:
    """Register the capsule tracing adapter under ``name`` in NeMo's registry.

    Thin wrapper over ``nemoguardrails.tracing.adapters.registry.register_log_adapter``
    — their extension point, called with our class. After this, any config may name
    the adapter in ``tracing.adapters``. Returns the registered class.
    """
    from nemoguardrails.tracing.adapters.registry import LogAdapterRegistry, register_log_adapter

    cls = get_capsule_log_adapter_class()
    # Their Registry raises ValueError on a duplicate name, so registering twice
    # (two recorders, or a module reimported under test) would take down the app
    # for no reason. Registration is idempotent here; a *different* class already
    # holding the name is a real conflict and still raises.
    try:
        existing = LogAdapterRegistry().get(name)
    except Exception:  # noqa: BLE001 — "not registered" is the normal path
        existing = None
    if existing is cls:
        return cls
    register_log_adapter(cls, name)
    return cls
