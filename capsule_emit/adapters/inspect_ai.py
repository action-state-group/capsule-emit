# SPDX-License-Identifier: Apache-2.0
"""Inspect (``inspect_ai``) eval-log adapter — one sealed record per model
call, one per tool call, one per sample's terminal state.

Loaded **out of tree**, read-only against a finished ``.eval`` log — there is
no live hook into Inspect's solver loop here, no fork, no CLA. Inspect
already writes a complete, structured transcript per sample
(``EvalSample.events``, including a ``ModelEvent`` for every call to the
model with the raw request/response the harness itself captured in
``ModelEvent.call``, and a ``ToolEvent`` for every tool call the harness
executed on the agent's behalf, with the harness's own ``function`` /
``arguments`` / ``result``). This adapter reads that transcript after a run
finishes and seals it: it does not change what Inspect records, and it
cannot see anything Inspect itself didn't keep.

Where the signing key lives — the harness boundary
----------------------------------------------------
Every capsule this module seals is signed by whatever key
``capsule_emit.signing.resolve_signer`` resolves for the ledger this adapter
was constructed with: an explicit ``signing_key_path``, else
``CAPSULE_SIGNING_KEY_PATH``, else ``<ledger>.signing_key.pem`` next to the
ledger file (0600, generate-on-first-use — see ``capsule_emit.signing``).
This module runs the sealing step; it does not run inside whatever sandbox
executed the agent's tool calls, and nothing here reads the key material
from anywhere the sandbox's filesystem or environment reaches. The
deployment invariant this depends on: the harness process's ledger path,
signing-key path, and ``CAPSULE_SIGNING_KEY_PATH`` env var must never be
inside a directory Inspect mounts into a sandbox (``local``/``docker``/etc.)
and must never be propagated into the sandboxed subprocess's environment. A
sandboxed process can run this same library and mint its own keypair — that
is not preventable and this module makes no claim otherwise — but it cannot
produce a record that verifies under the harness's own ``key_id``, because
it never had the harness's private key. See
``tests/test_inspect_ai_core.py::test_sandbox_process_cannot_sign_as_the_harness``
for the check; its docstring describes the key-into-sandbox mutant that
turns it red.

Why per-model-call, not just per-sample
----------------------------------------
A sample in Inspect can and often does call the model more than once (a
reasoning step, a tool round-trip, a re-ask). Sealing only the sample's final
output would commit to the *outcome* and throw away every intermediate call —
exactly the material an evaluator would want to cite when a transcript is
challenged. So this adapter walks ``EvalSample.events`` and seals ONE record
per ``ModelEvent`` found, chained (``confirms``) in the order Inspect recorded
them, and then ONE more record for the sample's terminal state (final
completion + scores, or its error), chained to the last model-call record.
A sample with zero model-call events (a sample that errored before any call
completed) still gets its terminal record; the chain simply has no
model-call parent.

What gets digested — exactly what the harness saw, nothing reconstructed
--------------------------------------------------------------------------
``ModelEvent.call.request`` / ``.response`` are Inspect's own record of the
raw bytes exchanged with the model API (``ModelCall.create()``, built by the
provider at call time — see ``inspect_ai.model._model_call``). This adapter
digests those dicts unmodified as ``agent_input`` / ``agent_output``. It
never reconstructs a request from ``ModelEvent.input`` (the higher-level
``ChatMessage`` list) and never fabricates a response when
``ModelEvent.call`` is absent or ``call.response`` is ``None`` (a call that
errored before returning) — the sealed record's ``tool_output`` is simply
absent in that case, and ``verdict``/``effect.status`` say ``errored``/
``failed`` so a reader sees the gap as a stated fact, not a filled-in guess.

Observation is post-hoc, and this module says so
--------------------------------------------------
This adapter runs against a **finished** ``.eval`` log, sometime after the
run completed — it is not wired into Inspect's own event stream and cannot
be (Inspect has no seal-at-call-time hook to attach to from outside its
process). Every record therefore carries ``observation_mode="post_hoc_event"``
and ``request_record_provenance`` naming this plainly, the same posture the
``litellm_listener`` adapter takes for its post-hoc half. The sealed record's
integrity claim is about the log entry, not about when it was witnessed: a
checkpoint registered against a public witness right after sealing still
bounds *that* time honestly (see ``capsule_emit.witness``); it says nothing
about how long the ``.eval`` log itself existed unwitnessed before this
adapter ran.

What this module does NOT establish — see also the example README
---------------------------------------------------------------------
Sealing a log's own account of itself is not a second, independent witness
to what the model actually said; it is a durable, checkable copy of what
Inspect's own harness captured. If Inspect's provider layer never captured a
raw request/response for some call, no digest exists for it here either —
absence in the source stays absence in the seal, never a default.

All sealing logic lives in the framework-lazy :class:`InspectAIListenerCore`
(usable without ``inspect_ai`` installed, given plain dicts); only
:func:`seal_eval_log` imports ``inspect_ai.log.read_eval_log``, and only when
called.
"""
from __future__ import annotations

from typing import Any

from ._base import CapsuleEmitterBase

__all__ = [
    "InspectAIListenerCore",
    "seal_eval_log",
    "OBSERVATION_MODE",
    "REQUEST_PROVENANCE",
]

#: Stamped on every capsule this adapter seals — see module docstring.
OBSERVATION_MODE = "post_hoc_event"

#: Why every capsule here is a post-hoc record, not a live commitment.
REQUEST_PROVENANCE = (
    "derived from a finished .eval log's own EvalSample.events record, read "
    "after the run completed; capsule-emit was not attached to Inspect's "
    "solver loop while it ran, and Inspect exposes no external seal-at-call "
    "hook for this adapter to use instead"
)


class InspectAIListenerCore(CapsuleEmitterBase):
    """Framework-lazy core: seals plain dicts extracted from an Inspect
    ``.eval`` log. Takes no dependency on ``inspect_ai`` itself — every
    argument here is already a JSON-safe dict/str/int, so this class is
    tested without the package: ``tests/test_inspect_ai_core.py`` runs in the
    default CI job and includes a check with ``import inspect_ai`` blocked.
    :func:`seal_eval_log` is the only place in this module that imports
    ``inspect_ai``; its tests need the ``inspect-ai`` extra.
    """

    def seal_model_call(
        self,
        *,
        sample_id: str,
        epoch: int,
        call_index: int,
        model_name: str | None,
        request: dict[str, Any],
        response: dict[str, Any] | None,
        error: str | None = None,
        prior_capsule_id: str | None = None,
    ):
        """Seal one ``ModelEvent`` as a record.

        ``request`` is always present (an event with no raw call at all is
        not sealed — see :func:`seal_eval_log`). ``response`` is ``None``
        exactly when Inspect's own record has none; ``tool_output`` is then
        omitted rather than forced to an empty dict, so an absent response
        reads as absent. The spec's confirmed-effect invariant (agent-action-
        capsule §5.2) requires a response digest for ``effect.status=
        "confirmed"``, so a response-less, non-errored call is sealed as
        ``"planned"`` (the profile's carve for "no confirmed effect") rather
        than a ``"confirmed"`` claiming a digest that does not exist.
        """
        provider, _, model_id = (model_name or "").partition("/")
        model = {
            "provider": provider or "unknown",
            "model_id": model_id or model_name or "unknown",
        }
        failed = bool(error)
        tool_output: dict[str, Any] | None
        if failed:
            tool_output = {"error": error}
            status = "failed"
        elif response is not None:
            tool_output = response
            status = "confirmed"
        else:
            tool_output = None
            status = "planned"

        return self.emit_capsule(
            action="inspect_eval_model_call",
            tool_input={
                "request": request,
                "observation_mode": OBSERVATION_MODE,
                "request_record_provenance": REQUEST_PROVENANCE,
            },
            tool_output=tool_output,
            verdict="errored" if failed else "executed",
            effect={"type": "inspect_model_call", "status": status},
            prior_capsule_id=prior_capsule_id,
            model=model,
            extra_compute={
                "inspect_sample_id": str(sample_id),
                "inspect_epoch": epoch,
                "inspect_call_index": call_index,
            },
        )

    def seal_tool_call(
        self,
        *,
        sample_id: str,
        epoch: int,
        call_index: int,
        tool_name: str | None,
        tool_call_id: str | None,
        arguments: dict[str, Any],
        result: Any = None,
        error: str | None = None,
        prior_capsule_id: str | None = None,
    ):
        """Seal one ``ToolEvent`` as a record: the harness's own record of a
        tool call it executed on the agent's behalf — ``function``/
        ``arguments`` in, ``result`` out, exactly as Inspect captured them.

        Same absence discipline as :meth:`seal_model_call`: ``result is
        None`` with no ``error`` seals ``"planned"`` (nothing confirmed
        yet) rather than fabricating a ``tool_output``; an ``error`` seals
        ``"failed"`` with the error message as the digested content, never a
        made-up success result.
        """
        failed = bool(error)
        tool_output: dict[str, Any] | None
        if failed:
            tool_output = {"error": error}
            status = "failed"
        elif result is not None:
            tool_output = {"result": result}
            status = "confirmed"
        else:
            tool_output = None
            status = "planned"

        return self.emit_capsule(
            action="inspect_eval_tool_call",
            tool_input={
                "tool_name": tool_name or "unknown",
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "observation_mode": OBSERVATION_MODE,
                "request_record_provenance": REQUEST_PROVENANCE,
            },
            tool_output=tool_output,
            verdict="errored" if failed else "executed",
            effect={"type": "inspect_tool_call", "status": status},
            prior_capsule_id=prior_capsule_id,
            extra_compute={
                "inspect_sample_id": str(sample_id),
                "inspect_epoch": epoch,
                "inspect_call_index": call_index,
                "inspect_tool_name": tool_name or "unknown",
                "inspect_tool_call_id": tool_call_id,
            },
        )

    def seal_sample_terminal(
        self,
        *,
        sample_id: str,
        epoch: int,
        sample_input: Any,
        target: Any,
        output_completion: str | None = None,
        scores: dict[str, Any] | None = None,
        error: str | None = None,
        prior_capsule_id: str | None = None,
    ):
        """Seal one sample's terminal state: what Inspect scored it as, or
        the error that stopped it. ``scores`` is omitted (never an empty
        placeholder) when the sample carries none; same for
        ``output_completion``. See :meth:`seal_model_call` for why a
        non-errored sample with nothing to report is sealed ``"planned"``,
        never a ``"confirmed"`` with no response digest behind it.
        """
        failed = bool(error)
        tool_output: dict[str, Any] = {}
        if failed:
            tool_output["error"] = error
        else:
            if output_completion is not None:
                tool_output["completion"] = output_completion
            if scores:
                tool_output["scores"] = scores
        tool_output = tool_output or None
        status = "failed" if failed else ("confirmed" if tool_output else "planned")

        return self.emit_capsule(
            action="inspect_eval_sample_result",
            tool_input={
                "sample_id": str(sample_id),
                "epoch": epoch,
                "input": sample_input,
                "target": target,
                "observation_mode": OBSERVATION_MODE,
            },
            tool_output=tool_output,
            verdict="errored" if failed else "executed",
            effect={"type": "inspect_sample_result", "status": status},
            prior_capsule_id=prior_capsule_id,
            extra_compute={
                "inspect_sample_id": str(sample_id),
                "inspect_epoch": epoch,
            },
        )


def _plain_input(sample_input: Any) -> Any:
    """``EvalSample.input`` is either a bare string or a list of
    ``ChatMessage`` pydantic objects — normalize the latter to plain dicts so
    every downstream digest step (``canonicalize_for_digest``) sees only
    JSON-safe values, never a pydantic object it would have to guess how to
    serialize."""
    if isinstance(sample_input, str):
        return sample_input
    return [m.model_dump(mode="json") for m in sample_input]


def _plain_score(score: Any) -> dict[str, Any]:
    return score.model_dump(mode="json", exclude_none=True, exclude={"history"})


def _plain_tool_result(result: Any) -> Any:
    """``ToolEvent.result`` is usually a plain str/JSON value but can be a
    richer content object depending on the tool. Digest what Inspect
    actually returned; fall back to ``str()`` only for a non-JSON-safe
    object rather than dropping it."""
    if result is None or isinstance(result, (str, int, float, bool, list, dict)):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return str(result)


def seal_eval_log(
    eval_log_path: str,
    *,
    operator: str,
    developer: str,
    ledger: str = "ledger.jsonl",
    core: InspectAIListenerCore | None = None,
    **core_kwargs: Any,
) -> dict[str, list]:
    """Read a finished Inspect ``.eval`` log and seal every sample in it.

    Per sample: one record per ``ModelEvent`` AND one per ``ToolEvent`` in
    ``sample.events``, in the order Inspect recorded them (a tool call and a
    model call in the same sample chain together, not in two separate
    chains), then one terminal record chained to the last of those (or
    unchained if the sample has no model-call or tool-call event at all).
    Returns ``{"<sample_id>#<epoch>": [EmitResult, ...]}`` in the same
    order.

    Lazy-imports ``inspect_ai.log.read_eval_log`` — this module, and the
    rest of ``capsule_emit``, never depends on ``inspect_ai`` being
    installed; only calling this function does.
    """
    from inspect_ai.log import read_eval_log

    log = read_eval_log(str(eval_log_path))
    core = core or InspectAIListenerCore(operator=operator, developer=developer, ledger=ledger, **core_kwargs)

    results: dict[str, list] = {}
    for sample in log.samples or []:
        sample_results: list = []
        prior_id: str | None = None
        call_index = 0

        for event in sample.events:
            kind = getattr(event, "event", None)
            if kind == "model":
                call = event.call
                if call is None or call.request is None:
                    # Inspect recorded no raw call at all for this event --
                    # there is nothing honest to digest, so nothing is
                    # sealed for it.
                    continue
                result = core.seal_model_call(
                    sample_id=sample.id,
                    epoch=sample.epoch,
                    call_index=call_index,
                    model_name=event.model,
                    request=call.request,
                    response=call.response,
                    error=event.error,
                    prior_capsule_id=prior_id,
                )
            elif kind == "tool":
                result = core.seal_tool_call(
                    sample_id=sample.id,
                    epoch=sample.epoch,
                    call_index=call_index,
                    tool_name=event.function,
                    tool_call_id=event.id,
                    arguments=event.arguments or {},
                    result=_plain_tool_result(event.result) if not event.error else None,
                    error=event.error.message if event.error else None,
                    prior_capsule_id=prior_id,
                )
            else:
                continue
            sample_results.append(result)
            prior_id = result.capsule_id
            call_index += 1

        sample_error = sample.error.message if sample.error else None
        scores = (
            {name: _plain_score(sc) for name, sc in sample.scores.items()}
            if sample.scores
            else None
        )
        terminal = core.seal_sample_terminal(
            sample_id=sample.id,
            epoch=sample.epoch,
            sample_input=_plain_input(sample.input),
            target=sample.target,
            output_completion=(sample.output.completion if sample.output and not sample_error else None),
            scores=scores,
            error=sample_error,
            prior_capsule_id=prior_id,
        )
        sample_results.append(terminal)
        results[f"{sample.id}#{sample.epoch}"] = sample_results

    return results
