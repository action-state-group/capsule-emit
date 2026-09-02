# SPDX-License-Identifier: Apache-2.0
"""LiteLLM proxy listener — request/outcome capsule pairs per LLM call.

Loaded **out of tree**: no fork of ``litellm``, no core PR, no CLA. The proxy's
own config-callback loader resolves a dotted string to an installed package::

    # config.yaml
    litellm_settings:
      callbacks: ["capsule_emit.adapters.litellm_listener.proxy_handler_instance"]

``litellm.proxy.types_utils.utils.get_instance_fn`` (line 9 of that module in the
released ``litellm==1.99.0`` wheel) splits the dotted string, imports the module
and ``getattr``s the last component. ``initialize_callbacks_on_proxy``
(``litellm/proxy/common_utils/callback_utils.py``) then hands the result to
``_loaded_callback_or_raise``, which **rejects a class** — only a ``CustomLogger``
*instance* or a plain callable is dispatchable. Hence ``proxy_handler_instance``:
a module-level instance, built on first attribute access from environment
variables, because the config path passes no constructor arguments.

Observation-only, deliberately
------------------------------
This listener implements exactly two hooks:

- ``async_log_success_event``      → the completed call
- ``async_post_call_failure_hook`` → the failed call

``async_pre_call_hook`` is **not** implemented and must not be added here. That
hook is deny-capable — its own signature comment says it may *"raise exception if
invalid, return a str for the user to receive - if rejected"* — and denial is a
gate concern, not a record concern. Likewise ``async_post_call_failure_hook`` can
return an ``HTTPException`` to rewrite the error the client sees (first callback
to do so wins, ``litellm/proxy/utils.py``); this listener always returns ``None``.
The record layer observes; it never changes what the operator's users experience.

Chain shape
-----------
Each event seals **two** capsules, chained:

===========================  ==============================================
``effect.status="planned"``    the request half — the chain parent
``effect.status="confirmed"``  the response half (``async_log_success_event``)
``effect.status="failed"``     the exception half (``async_post_call_failure_hook``)
===========================  ==============================================

Both are sealed inside one handler call, so the parent id is a local variable:
the link is structural, and there is no pending map and no pairing heuristic to
get wrong under concurrency (same property as the agno listener).

**The request capsule is post-hoc and says so.** ``effect.status="planned"`` is
the profile's carve for *"this record asserts no execution"* (§5.2 —
``derive_effect_mode`` maps it to ``effect_mode="not_applicable"``), which is
exactly what the request half claims. It does **not** encode *when* the record
was sealed, and this adapter does not let the reader assume it: every capsule
carries ``observation_mode="post_hoc_event"`` and the request half additionally
carries ``request_record_provenance``.

The reason it cannot be better than post-hoc: litellm dispatches no
observation-only pre-call hook. ``CustomLogger.async_log_pre_api_call`` is
*declared* but has **zero call sites** in the released 1.99.0 wheel, and the sync
``log_pre_api_call`` that does fire would put ledger I/O on the request path.
A record sealed before execution is therefore not available here and is not
claimed — a test pins the dead hook so the day litellm starts dispatching it,
this adapter finds out.

Redaction interplay — we digest what the operator's redaction produced
---------------------------------------------------------------------
On the **success** path litellm runs *every* registered callback's
``async_logging_hook`` to completion before it dispatches *any*
``async_log_success_event`` (``litellm_core_utils/litellm_logging.py``,
``async_success_handler``), threading each hook's return value into the shared
``model_call_details``/``result``. Measured, not inferred: a sibling redaction
callback's output is what reaches this listener, regardless of registration
order. So the success capsules commit to the **redacted view** — the record as
the operator's pipeline produced it — not to the wire payload. That is the
honest claim for a receipt: it proves what was logged, and an auditor verifies it
against the same redacted log.

The **failure** path is asymmetric and this adapter does not paper over it.
``ProxyLogging.post_call_failure_hook`` dispatches ``async_post_call_failure_hook``
with the raw proxy ``request_data``; it does **not** route it through
``async_logging_hook`` or ``redact_message_input_output_from_logging``. Digesting
the prompt there would commit to an *un-redacted* preimage that no redacted log
can reproduce. So by default the failure path seals an allowlisted view **without**
the messages, and stamps ``request_payload_withheld`` plus the reason. Absent is
recorded as absent, never quietly passed off as empty. Set
``include_unredacted_failure_payload=True`` to opt in.

Secrets are never in the allowlist
----------------------------------
``kwargs["litellm_params"]`` carries ``api_key``, ``azure_password``,
``client_secret`` and friends. Capsules are digest-only, so nothing here would
*store* a key — but a digest is still a commitment to a preimage that a verifier
must be handed. Only :data:`_REQUEST_FIELDS`, one :data:`_PROMPT_FIELDS` entry and
:data:`_RESPONSE_FIELDS` ever reach the digest layer, and a test asserts no
credential-shaped key is among them.

All sealing logic lives in the framework-free :class:`LiteLLMListenerCore` (fully
testable without ``litellm`` installed); :class:`LiteLLMCapsuleListener` binds it
to ``litellm.integrations.custom_logger.CustomLogger``.

Requires ``pip install "capsule-emit[litellm]"``. Verified against the released
``litellm==1.99.0`` wheel. Note that wheel declares ``Requires-Python: >=3.10``
but imports ``typing.NotRequired``, so it only imports on 3.11+ — see the adapter
docs page.
"""
from __future__ import annotations

import os
import warnings
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = [
    "LiteLLMCapsuleListener",
    "LiteLLMListenerCore",
    "listener_from_env",
    "OBSERVATION_MODE",
    "REQUEST_PROVENANCE",
    "WITHHELD_REASON",
]

#: Stamped on every capsule this adapter seals. Not a quality score — provenance:
#: the capsule states how it observed and the consumer decides the weight.
OBSERVATION_MODE = "post_hoc_event"

#: Why the request capsule is not a pre-execution commitment record.
REQUEST_PROVENANCE = (
    "derived from the completed call's log record, not witnessed before execution; "
    "litellm dispatches no observation-only pre-call hook "
    "(CustomLogger.async_log_pre_api_call has no call sites in litellm 1.99.0)"
)

#: Why the failure path withholds the prompt by default.
WITHHELD_REASON = (
    "litellm dispatches async_post_call_failure_hook with the raw proxy request_data, "
    "bypassing async_logging_hook redaction; digesting it would commit to a preimage "
    "no redacted log can reproduce. Set include_unredacted_failure_payload=True to include it."
)

#: The only request-side keys that ever reach the digest layer. Never widen this
#: to ``litellm_params`` or ``optional_params`` — both carry provider credentials.
#: The prompt is NOT here; it is added separately, exactly once, from
#: :data:`_PROMPT_FIELDS`.
_REQUEST_FIELDS = ("model", "call_type", "user", "stream")

#: litellm carries the same prompt under more than one key: ``messages`` for chat,
#: ``input`` for embeddings/older shapes, ``prompt`` for text completion. Its own
#: ``perform_redaction`` clears all three, but a *custom* ``async_logging_hook``
#: that rewrites only ``messages`` — the obvious thing to write — leaves the
#: others holding the original text. Sealing more than one would let the least
#: redacted copy decide the digest, quietly undoing the operator's redaction. So
#: exactly one is sealed: the first of these present, named in ``prompt_field``
#: so the preimage stays reconstructible.
_PROMPT_FIELDS = ("messages", "input", "prompt")

#: The only response-side keys that ever reach the digest layer.
_RESPONSE_FIELDS = ("id", "model", "object", "created", "choices", "usage")

#: Substrings that must never appear in a sealed request/response key.
_CREDENTIAL_MARKERS = ("api_key", "password", "secret", "token", "credential")

#: Fallback only — used when litellm does not supply ``custom_llm_provider``.
#: Matched against the model id, which in litellm is either ``provider/model`` or
#: a bare vendor model name, so both spellings are listed.
_PROVIDER_HINTS = (
    ("anthropic", "anthropic"),
    ("claude", "anthropic"),
    ("azure", "azure"),
    ("bedrock", "aws"),
    ("vertex", "google"),
    ("gemini", "google"),
    ("palm", "google"),
    ("openai", "openai"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("ollama", "ollama"),
    ("mistral", "mistral"),
    ("command-", "cohere"),
    ("cohere", "cohere"),
)


def _truncate(value: Any, limit: int) -> Any:
    """Bound a payload so one huge prompt cannot bloat the ledger.

    Truncation is *recorded*: the marker replaces the value rather than silently
    shortening it, so a verifier can never mistake a clipped digest for a
    faithful one. Only the payload itself is bounded — the identifying scalars
    (model, call_type, response id, usage) always survive, so an oversized call
    still produces a receipt that says which model was called.
    """
    if limit <= 0:
        return value
    try:
        rendered = repr(value)
    except Exception:  # noqa: BLE001 — a framework object with a hostile __repr__
        return {"capsule_emit_unrenderable": True}
    if len(rendered) <= limit:
        return value
    return {"capsule_emit_truncated": True, "original_repr_chars": len(rendered)}


def _plain(value: Any) -> Any:
    """Best-effort JSON-shaped view of a litellm response object.

    litellm returns pydantic models (``ModelResponse``); ``model_dump`` gives the
    dict the digest layer needs. Anything that will not convert is reduced to its
    string form rather than dropped, so the field stays present and honest.
    """
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 — third-party model, any failure mode
                pass
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, float):
        return value  # canonicalized by the base funnel (RFC 8785 §3.2.2.3)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def _provider_from(kwargs: dict, model: str | None) -> str:
    explicit = kwargs.get("custom_llm_provider")
    if explicit:
        return str(explicit)
    probe = (model or "").lower()
    for needle, provider in _PROVIDER_HINTS:
        if needle in probe:
            return provider
    return "unknown"


class LiteLLMListenerCore(CapsuleEmitterBase):
    """Framework-free core of the LiteLLM listener.

    Args:
        include_request_record: Seal the ``requested`` capsule that the outcome
            capsule chains to (default True). With it off there is no chain
            parent and the outcome capsule stamps ``unchained_reason``.
        include_unredacted_failure_payload: Include ``messages``/``input`` in the
            failure path's request capsule (default False). See
            :data:`WITHHELD_REASON` — the failure hook bypasses litellm's
            ``async_logging_hook`` redaction.
        max_payload_chars: Bound on the ``repr`` size of the sealed prompt (and of
            the response ``choices``) before it is replaced by a recorded
            truncation marker (default 20000). ``0`` disables the bound. The
            identifying scalars are never truncated.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer,
            ledger, anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        include_request_record: bool = True,
        include_unredacted_failure_payload: bool = False,
        max_payload_chars: int = 20000,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._include_request_record = include_request_record
        self._include_unredacted_failure_payload = include_unredacted_failure_payload
        self._max_payload_chars = max_payload_chars

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the proxy)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: LiteLLM listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    def _compute(self, **extra: Any) -> dict[str, Any]:
        stamp: dict[str, Any] = {"observation_mode": OBSERVATION_MODE}
        stamp.update({k: v for k, v in extra.items() if v is not None})
        return stamp

    def _request_view(self, source: dict, *, withhold: bool) -> dict[str, Any]:
        view: dict[str, Any] = {
            k: _plain(source.get(k)) for k in _REQUEST_FIELDS if source.get(k) is not None
        }
        if not withhold:
            for field in _PROMPT_FIELDS:
                if source.get(field) is not None:
                    view["prompt_field"] = field
                    view["prompt"] = _truncate(_plain(source[field]), self._max_payload_chars)
                    break
        return view

    @staticmethod
    def _call_id(source: dict) -> str | None:
        direct = source.get("litellm_call_id")
        if direct:
            return str(direct)
        params = source.get("litellm_params")
        if isinstance(params, dict) and params.get("litellm_call_id"):
            return str(params["litellm_call_id"])
        return None

    def _seal_request(
        self,
        source: dict,
        *,
        call_type: str,
        model_info: dict[str, str],
        call_id: str | None,
        withhold: bool,
    ) -> str | None:
        """Seal the ``planned`` (request) half and return its capsule id (or None)."""
        if not self._include_request_record:
            return None
        result = self._seal(
            action=f"litellm.{call_type}",
            tool_input=self._request_view(source, withhold=withhold),
            effect={"type": "llm_call", "status": "planned"},
            action_type="fyi",
            runtime="litellm",
            model=model_info,
            extra_compute=self._compute(
                request_record_provenance=REQUEST_PROVENANCE,
                litellm_call_id=call_id,
                request_payload_withheld=True if withhold else None,
                withheld_reason=WITHHELD_REASON if withhold else None,
            ),
        )
        return None if result is None else result.capsule_id

    def _unchained(self, parent_id: str | None) -> str | None:
        """Reason stamp when the outcome record has no committed parent."""
        if parent_id is not None:
            return None
        if not self._include_request_record:
            return "request capsule disabled by configuration (include_request_record=False)"
        return (
            "request capsule could not be sealed for this call; this outcome record "
            "has no parent and is not evidence of a recorded request"
        )

    # -- success -----------------------------------------------------------

    def on_success_core(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any = None,
        end_time: Any = None,
    ) -> None:
        """Seal ``planned`` → ``confirmed`` for one completed call.

        ``kwargs`` is litellm's ``model_call_details`` **after** every registered
        callback's ``async_logging_hook`` has run, so the payload sealed here is
        the operator's redacted view.
        """
        kwargs = kwargs if isinstance(kwargs, dict) else {}
        model = kwargs.get("model")
        call_type = str(kwargs.get("call_type") or "completion")
        model_info = {
            "provider": _provider_from(kwargs, model if isinstance(model, str) else None),
            "model_id": str(model) if model is not None else "unknown",
        }
        call_id = self._call_id(kwargs)

        parent_id = self._seal_request(
            kwargs,
            call_type=call_type,
            model_info=model_info,
            call_id=call_id,
            withhold=False,
        )

        response = _plain(response_obj)
        if isinstance(response, dict):
            response = {k: response[k] for k in _RESPONSE_FIELDS if k in response}
            if "choices" in response:
                response["choices"] = _truncate(response["choices"], self._max_payload_chars)
        else:
            response = _truncate(response, self._max_payload_chars)
        self._seal(
            action=f"litellm.{call_type}",
            tool_output=response,
            effect={"type": "llm_call", "status": "confirmed"},
            prior_capsule_id=parent_id,
            action_type="fyi",
            runtime="litellm",
            model=model_info,
            extra_compute=self._compute(
                litellm_call_id=call_id,
                unchained_reason=self._unchained(parent_id),
            ),
        )

    # -- failure -----------------------------------------------------------

    def on_failure_core(
        self,
        request_data: dict,
        original_exception: BaseException | None,
        traceback_str: str | None = None,
    ) -> None:
        """Seal ``planned`` → ``failed`` for one failed call.

        ``request_data`` is the raw proxy request body — **not** redacted; see
        :data:`WITHHELD_REASON`.
        """
        request_data = request_data if isinstance(request_data, dict) else {}
        model = request_data.get("model")
        call_type = str(request_data.get("call_type") or "completion")
        model_info = {
            "provider": _provider_from(request_data, model if isinstance(model, str) else None),
            "model_id": str(model) if model is not None else "unknown",
        }
        call_id = self._call_id(request_data)
        withhold = not self._include_unredacted_failure_payload

        parent_id = self._seal_request(
            request_data,
            call_type=call_type,
            model_info=model_info,
            call_id=call_id,
            withhold=withhold,
        )

        self._seal(
            action=f"litellm.{call_type}",
            tool_output=None if original_exception is None else str(original_exception),
            verdict="errored",
            effect={"type": "llm_call", "status": "failed"},
            prior_capsule_id=parent_id,
            action_type="fyi",
            runtime="litellm",
            model=model_info,
            extra_compute=self._compute(
                litellm_call_id=call_id,
                exception_class=(
                    None if original_exception is None else type(original_exception).__name__
                ),
                traceback_recorded=traceback_str is not None,
                unchained_reason=self._unchained(parent_id),
            ),
        )


try:
    from litellm.integrations.custom_logger import CustomLogger as _Base

    _HAVE_LITELLM = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _Base = object  # type: ignore[assignment,misc]
    _HAVE_LITELLM = False


class LiteLLMCapsuleListener(_Base):  # type: ignore[valid-type,misc]
    """``CustomLogger`` shell binding litellm's two observation hooks to the core.

    Register through the proxy config (see the module docstring) or, in plain
    Python, by appending an instance to ``litellm.callbacks``.
    """

    def __init__(self, **core_kw: Any) -> None:
        if not _HAVE_LITELLM:
            raise ImportError(
                "LiteLLMCapsuleListener needs litellm. "
                'Install with: pip install "capsule-emit[litellm]"'
            )
        super().__init__()
        self.core = LiteLLMListenerCore(**core_kw)

    async def async_log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.core.on_success_core(kwargs, response_obj, start_time, end_time)

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any = None,
        traceback_str: str | None = None,
    ) -> None:
        """Record the failure and return ``None``.

        Returning an ``HTTPException`` here would rewrite the error the client
        receives (``litellm/proxy/utils.py``: first callback to return or raise
        one wins). This listener is observation-only and must always return
        ``None`` — a test asserts it.
        """
        self.core.on_failure_core(request_data, original_exception, traceback_str)
        return None


def listener_from_env(**overrides: Any) -> LiteLLMCapsuleListener:
    """Build a listener from environment variables (the config.yaml load path).

    ``litellm_settings.callbacks`` passes no constructor arguments, so operator
    identity comes from the environment:

    ==============================  ========================================
    ``CAPSULE_EMIT_OPERATOR``       required — tenant/org on every capsule
    ``CAPSULE_EMIT_DEVELOPER``      required — agent name + version
    ``CAPSULE_EMIT_LEDGER``         ledger path (default ``ledger.jsonl``)
    ``CAPSULE_EMIT_LITELLM_REQUEST_RECORD``       ``0`` to disable the chain parent
    ``CAPSULE_EMIT_LITELLM_FAILURE_PAYLOAD``      ``1`` to opt into un-redacted
                                                  failure prompts
    ``CAPSULE_EMIT_LITELLM_MAX_PAYLOAD_CHARS``    payload bound (default 20000)
    ==============================  ========================================

    The two required variables raise rather than defaulting: a ledger full of
    capsules stamped ``operator="unknown"`` is worse evidence than a proxy that
    refuses to start and says which variable is missing.
    """
    missing = [k for k in ("CAPSULE_EMIT_OPERATOR", "CAPSULE_EMIT_DEVELOPER") if not os.environ.get(k)]
    if missing:
        raise ValueError(
            "capsule-emit litellm listener: missing required environment "
            f"variable(s) {', '.join(missing)}. Set them alongside the "
            'litellm_settings.callbacks entry; they identify who the receipts '
            "belong to and are not guessed."
        )
    kw: dict[str, Any] = {
        "operator": os.environ["CAPSULE_EMIT_OPERATOR"],
        "developer": os.environ["CAPSULE_EMIT_DEVELOPER"],
        "ledger": os.environ.get("CAPSULE_EMIT_LEDGER", "ledger.jsonl"),
        "include_request_record": os.environ.get("CAPSULE_EMIT_LITELLM_REQUEST_RECORD", "1") != "0",
        "include_unredacted_failure_payload": os.environ.get(
            "CAPSULE_EMIT_LITELLM_FAILURE_PAYLOAD", "0"
        )
        == "1",
        "max_payload_chars": int(os.environ.get("CAPSULE_EMIT_LITELLM_MAX_PAYLOAD_CHARS", "20000")),
    }
    kw.update(overrides)
    return LiteLLMCapsuleListener(**kw)


def __getattr__(name: str) -> Any:
    """Build ``proxy_handler_instance`` on attribute access, not on import.

    ``get_instance_fn`` resolves the dotted path with ``getattr(module, name)``,
    so a module-level ``__getattr__`` (PEP 562) is enough to satisfy the loader
    while leaving ``import capsule_emit.adapters.litellm_listener`` free of
    side effects and usable in tests with no environment set.
    """
    if name == "proxy_handler_instance":
        return listener_from_env()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
