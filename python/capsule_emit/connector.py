# SPDX-License-Identifier: Apache-2.0
"""``ConnectorPort`` — the informal adapter contract, made explicit.

Every adapter under :mod:`capsule_emit.adapters` already does two things, in
some adapter-specific way: it decides whether an observed event is a mere
observation or a consequential effect (the two-signal rule,
``docs/whats-consequential.md``), and it turns the events worth recording
into a :data:`~capsule_emit.surface.Capsule` — minted here (``seal()``) or
carried in already-signed (``received()``). Fifteen adapters each wrote that
logic by hand, informally, with no shared name for it.

:class:`ConnectorPort` names that contract as a :class:`typing.Protocol`
rather than a base class an adapter must inherit from — every existing
adapter keeps its own constructor and call shape; conforming means adding
``classify()``/``capture()``/``boundary_class`` that a caller (or a test)
can check with ``isinstance(adapter, ConnectorPort)``.

    from capsule_emit.connector import ConnectorEvent, ConnectorPort

    event = ConnectorEvent(name="get_weather", http_method="GET",
                            mcp_read_only_hint=True)
    if isinstance(emitter, ConnectorPort):
        classification = emitter.classify(event)   # Classification.OBSERVATION
        if classification is Classification.EFFECT or event.resource_sensitive:
            capsule = emitter.capture(event)        # seal() or received()

This module holds the shared vocabulary only. Retrofitting an adapter is
opt-in and deliberately incremental — two adapters conform today
(:class:`~capsule_emit.adapters.mcp.MCPCapsuleEmitter`, the boundary-capture
case, and :class:`~capsule_emit.adapters.langchain_listener.LangChainCapsuleListener`,
the listener case); the rest keep their existing, adapter-specific surface
unchanged. Conformance is additive per adapter, never assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .core import EmitResult

__all__ = [
    "BoundaryClass",
    "Classification",
    "ConnectorEvent",
    "ConnectorPort",
    "classify_signal_1",
]

#: Alias, not a new type -- the noun this module hands back is the same
#: Capsule (= EmitResult) that seal()/received() return (surface.py).
Capsule = EmitResult


class Classification(str, Enum):
    """The two-signal rule's output for one event (``docs/whats-consequential.md``).

    ``EFFECT`` — the event crosses a system boundary carrying a write: it
    mutates state outside the process, moves money, creates an obligation, or
    grants access. Always worth capturing.

    ``OBSERVATION`` — a read. Worth capturing only when it also touches a
    resource tagged sensitive (Signal 2 — see :attr:`ConnectorEvent.resource_sensitive`);
    otherwise it belongs to the observability/OTel layer this record system
    composes with, not this port.
    """

    EFFECT = "effect"
    OBSERVATION = "observation"


class BoundaryClass(str, Enum):
    """Where in the call graph a :class:`ConnectorPort` adapter sits — the
    layering table in ``docs/whats-consequential.md``. A declared, not
    inferred, property of the adapter instance: the same event can arrive at
    a gateway, a decorator, or an engine, and what each layer is *able* to
    see (Signal 1 only, vs. Signal 1 + Signal 2) differs by where it sits,
    not by the event itself.
    """

    #: Sees traffic, not data tags — e.g. the agentgateway ``mcpGuardrails``
    #: hook. Evaluates Signal 1 only.
    GATEWAY = "gateway"
    #: Developer-explicit wrapping at the tool call site — e.g.
    #: ``@emitter.tool()``. Evaluates whatever the developer hands it.
    DECORATOR = "decorator"
    #: A passive tap on a framework's own callback/event stream — e.g. a
    #: LangChain or CrewAI listener. Sees every call the framework runs,
    #: not only the ones a developer chose to wrap.
    LISTENER = "listener"
    #: Knows the resource classification. The only layer that can evaluate
    #: Signal 2 (privileged reads) directly.
    ENGINE = "engine"


@dataclass(frozen=True)
class ConnectorEvent:
    """The minimal, adapter-agnostic shape a :class:`ConnectorPort` needs to
    classify and capture one observed call.

    Every field mirrors a row of the Signal 1 table in
    ``docs/whats-consequential.md`` or its Signal 2 paragraph — a
    ``ConnectorEvent`` is that table's runtime signals, named once instead of
    re-read ad hoc per adapter. ``None`` means "this runtime does not carry
    that signal" (distinct from ``False``, which means "the signal is
    present and says no").

    Attributes:
        name: The action/tool name, for the capsule's ``action`` field and
            for error messages. Required — even a foreign carried artifact
            needs a name for the log.
        tool_input: The call's input, when this event will be ``seal()``-ed
            (``event.foreign`` is ``False``). Ignored for a foreign carry.
        tool_output: The call's output, when this event will be ``seal()``-ed.
        http_method: The HTTP verb, when the runtime is HTTP-shaped
            (``"GET"``/``"HEAD"``/``"OPTIONS"`` read; others write).
        mcp_read_only_hint: MCP's ``readOnlyHint`` tool annotation, when
            present. A hint, not enforcement — see the module-level note on
            fail-safe defaults below.
        mcp_destructive_hint: MCP's ``destructiveHint`` tool annotation.
        commit_step_present: Whether the runtime reports an explicit commit
            step (a signal some non-HTTP, non-MCP runtimes carry directly).
        resource_sensitive: Signal 2 — is the read's target resource tagged
            sensitive (PHI/PII/regulated/secrets)? Only ever meaningful for
            an :attr:`~Classification.OBSERVATION` (a write is captured
            regardless); engine-side only per the taxonomy doc, since only
            the engine knows the resource's classification tag. ``None``
            (unknown) is treated as not-sensitive by :func:`classify_signal_1`
            — Signal 2 does not widen Signal 1's fail-safe default, it is a
            separate, additive reason to capture an observation.
        foreign: When ``True``, this event is someone else's already-signed
            artifact — :meth:`ConnectorPort.capture` must call ``received()``,
            never ``seal()`` (see ``surface.py``'s dispatch rule). When
            ``False`` (default), the event is content this adapter's
            operator authored and ``capture()`` calls ``seal()``.
        foreign_bytes: The exact transmitted bytes/str, when ``foreign`` is
            ``True``. Required in that case.
        foreign_type: The foreign artifact's own registered CPB type (the
            ``type=`` ``received()`` requires). Required when ``foreign`` is
            ``True``.
    """

    name: str
    tool_input: Any = None
    tool_output: Any = None
    http_method: str | None = None
    mcp_read_only_hint: bool | None = None
    mcp_destructive_hint: bool | None = None
    commit_step_present: bool | None = None
    resource_sensitive: bool | None = None
    foreign: bool = False
    foreign_bytes: bytes | bytearray | memoryview | str | None = None
    foreign_type: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ConnectorEvent.name must be a non-empty string")
        if self.foreign:
            if self.foreign_bytes is None:
                raise ValueError("ConnectorEvent(foreign=True) requires foreign_bytes")
            if not self.foreign_type or not self.foreign_type.strip():
                raise ValueError("ConnectorEvent(foreign=True) requires a non-empty foreign_type")


_READ_ONLY_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def classify_signal_1(event: ConnectorEvent) -> Classification:
    """The two-signal rule's Signal 1, read mechanically off a
    :class:`ConnectorEvent` — ``docs/whats-consequential.md``.

    Command-Query Separation, in priority order, first signal present wins:

    1. ``destructiveHint=True`` is an unambiguous command — MCP's own
       framing names it a stronger claim than ``readOnlyHint``.
    2. ``readOnlyHint`` decides directly when present (``True`` → query,
       anything else, including an explicit ``False`` → command).
    3. ``http_method`` decides when present (safe methods per RFC 9110
       §9.2.1 → query, everything else → command).
    4. ``commit_step_present`` decides when present.

    **Unknown defaults to :attr:`Classification.EFFECT` — fail-safe, never
    fail-open.** When none of the four signals is present, this function
    cannot determine the effect and classifies the event as consequential
    rather than guessing it is a harmless read; over-gating a genuinely safe
    call is the correct cost (see the taxonomy doc's own statement of this
    rule). A trusted hint may only ever *downgrade* toward
    :attr:`~Classification.OBSERVATION`; absence of every signal never does.

    Signal 2 (``resource_sensitive``) is deliberately not folded in here —
    it can only ever add a reason to *capture* an observation, never change
    whether the event *is* one; see :attr:`ConnectorEvent.resource_sensitive`.
    """
    if event.mcp_destructive_hint:
        return Classification.EFFECT
    if event.mcp_read_only_hint is not None:
        return Classification.OBSERVATION if event.mcp_read_only_hint else Classification.EFFECT
    if event.http_method is not None:
        method = event.http_method.upper()
        return Classification.OBSERVATION if method in _READ_ONLY_HTTP_METHODS else Classification.EFFECT
    if event.commit_step_present is not None:
        return Classification.EFFECT if event.commit_step_present else Classification.OBSERVATION
    return Classification.EFFECT


@runtime_checkable
class ConnectorPort(Protocol):
    """The adapter contract every ``capsule_emit.adapters`` module already
    implements informally, named once so it can be checked
    (``isinstance(adapter, ConnectorPort)``) rather than read off each
    adapter's source.

    Attributes:
        boundary_class: One of :class:`BoundaryClass`'s values — declared by
            the adapter, never inferred by this port, since it describes
            *where the adapter sits*, not anything about a given event.

    Methods:
        classify: ``event -> observation | effect`` — the two-signal rule's
            Signal 1 (:func:`classify_signal_1`); an adapter MAY layer its
            own Signal 2 on top when it has resource-classification
            knowledge (the ``ENGINE`` boundary class), but MUST NOT loosen
            Signal 1's fail-safe default.
        capture: ``event -> seal() | received()`` — mints a capsule for
            content this adapter's operator authored (``event.foreign`` is
            ``False``), or carries in an already-signed foreign artifact
            (``event.foreign`` is ``True``). Always returns an already-logged
            :data:`Capsule`; never a bare dict, never ``None``.
    """

    boundary_class: str

    def classify(self, event: ConnectorEvent) -> Classification:
        ...  # pragma: no cover

    def capture(self, event: ConnectorEvent) -> Capsule:
        ...  # pragma: no cover
