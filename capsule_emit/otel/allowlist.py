# SPDX-License-Identifier: Apache-2.0
"""The `org.agentactioncapsule.otel` allow-list — draft-palanisamy-scitt-aac-otel-00.

Every table here is transcribed field-for-field from the draft's "The
`org.agentactioncapsule.otel` Block", "Resource attributes", and "GenAI
semantic conventions" sections (`_work/palanisamy-ep-drafts/draft-palanisamy-
scitt-aac-otel-00.md` in the workspace at the time this was written — the
draft is "under review", not yet an RFC, so this module cites section titles
rather than a stable URL). **Allow-list, default-deny**: a key absent from a
table below never leaves this process, full stop — see
:func:`capsule_emit.otel.block.build_otel_block`, which looks up every
incoming attribute in these tables and drops anything not found, no
exceptions.

Three tiers, transcribed from the draft's own vocabulary (not this
extension's invention — see `I-D.mih-scitt-agent-action-capsule`,
"Data-Admission Tiers"):

- ``CLEAR_SAFE`` — the value MAY leave the process as-is.
- ``DIGEST_ONLY`` — only ``SHA-256(value)`` may leave; the raw value never
  does.
- ``NEVER_ENTERS`` — the key is named here ONLY so the leak-mutant test
  (``tests/test_otel_processor.py``) has a real, spec-cited row to plant a
  broken allow-list entry for. A ``NEVER_ENTERS`` row is never read as
  permission to emit anything — :func:`capsule_emit.otel.block.build_otel_block`
  only ever emits a ``CLEAR_SAFE`` or ``DIGEST_ONLY`` key.

**v0's own conservative choice, on top of the draft.** The draft leaves
``trace_id``/``span_id``/``parent_span_id``/``span_name`` as clear-safe
*conditional on a producer privacy assessment* it cannot verify mechanically
(are the systems these IDs index into free of end-user data?). This module
does not attempt that assessment; the block builder defaults every one of
those fields to ``DIGEST_ONLY`` and only emits them clear when the caller
passes ``clear_trace_context=True`` — an explicit, per-deployment opt-in
after doing the draft's own required assessment, never a default.
"""
from __future__ import annotations

import re
from enum import Enum

__all__ = [
    "Tier",
    "RESOURCE_ATTRS",
    "SEMCONV_ATTRS",
    "NEVER_ENTERS_SEMCONV",
    "NEVER_ENTERS_SEMCONV_PREFIXES",
    "DEFAULT_SEMCONV_SOURCE",
    "TRACE_ID_RE",
    "SPAN_ID_RE",
    "TRACE_FLAGS_RE",
    "tier_for_semconv_attribute",
]


class Tier(str, Enum):
    CLEAR_SAFE = "clear_safe"
    DIGEST_ONLY = "digest_only"
    NEVER_ENTERS = "never_enters"


#: The draft's "GenAI semantic conventions" commit this table was transcribed
#: against — carried in the block's ``semconv.source`` field per the draft's
#: own requirement ("MUST name the repository and the commit or release").
DEFAULT_SEMCONV_SOURCE = "open-telemetry/semantic-conventions-genai@8c1b98a"

#: Resource attributes table (draft, "Resource attributes"). Deliberately
#: narrower than the draft's own ceiling: the draft lists
#: ``service.instance.id``/``host.name``/``host.id``/``container.id`` as
#: "digest-only at most and SHOULD be omitted" (small effective entropy,
#: hashing is not anonymization). v0 takes the SHOULD literally and omits
#: them outright rather than shipping a digest-only path for values the
#: draft itself recommends never emitting.
RESOURCE_ATTRS: dict[str, Tier] = {
    "service.name": Tier.CLEAR_SAFE,
    "service.version": Tier.CLEAR_SAFE,
    "service.namespace": Tier.CLEAR_SAFE,
    "deployment.environment.name": Tier.CLEAR_SAFE,
    "telemetry.sdk.name": Tier.CLEAR_SAFE,
    "telemetry.sdk.version": Tier.CLEAR_SAFE,
}

#: GenAI semantic-convention attributes table (draft, "GenAI semantic
#: conventions"). Every CLEAR_SAFE/DIGEST_ONLY row here is transcribed from
#: the draft's disposition column; rows the draft marks "out of scope" or
#: leaves unclassified are simply absent (default-deny already covers them —
#: they do not need a tier).
SEMCONV_ATTRS: dict[str, Tier] = {
    # clear-safe
    "gen_ai.provider.name": Tier.CLEAR_SAFE,
    "gen_ai.request.model": Tier.CLEAR_SAFE,
    "gen_ai.response.model": Tier.CLEAR_SAFE,
    "gen_ai.operation.name": Tier.CLEAR_SAFE,
    "gen_ai.agent.id": Tier.CLEAR_SAFE,
    "gen_ai.agent.name": Tier.CLEAR_SAFE,
    "gen_ai.agent.version": Tier.CLEAR_SAFE,
    # clear-safe, conditional (producer-hygiene conditions the draft states
    # in prose — "fixed workflow names only", "tool identity, not
    # arguments", "non-identifying" — not mechanically checkable here; see
    # the module docstring for the one condition this module DOES enforce
    # mechanically, trace/span identifiers).
    "gen_ai.workflow.name": Tier.CLEAR_SAFE,
    "gen_ai.tool.name": Tier.CLEAR_SAFE,
    "gen_ai.tool.type": Tier.CLEAR_SAFE,
    "gen_ai.tool.call.id": Tier.CLEAR_SAFE,
    "gen_ai.usage.input_tokens": Tier.CLEAR_SAFE,
    "gen_ai.usage.output_tokens": Tier.CLEAR_SAFE,
    "gen_ai.usage.reasoning.output_tokens": Tier.CLEAR_SAFE,
    "gen_ai.usage.cache_read.input_tokens": Tier.CLEAR_SAFE,
    "gen_ai.usage.cache_write.input_tokens": Tier.CLEAR_SAFE,
    "gen_ai.request.temperature": Tier.CLEAR_SAFE,
    "gen_ai.request.top_p": Tier.CLEAR_SAFE,
    "gen_ai.request.top_k": Tier.CLEAR_SAFE,
    "gen_ai.request.max_tokens": Tier.CLEAR_SAFE,
    "gen_ai.request.seed": Tier.CLEAR_SAFE,
    "gen_ai.request.stop_sequences": Tier.CLEAR_SAFE,
    "gen_ai.request.reasoning.level": Tier.CLEAR_SAFE,
    "gen_ai.request.choice.count": Tier.CLEAR_SAFE,
    "gen_ai.response.finish_reasons": Tier.CLEAR_SAFE,
    "gen_ai.response.status": Tier.CLEAR_SAFE,
    "gen_ai.output.type": Tier.CLEAR_SAFE,
    "gen_ai.data_source.id": Tier.CLEAR_SAFE,
    "gen_ai.memory.store.id": Tier.CLEAR_SAFE,
    "gen_ai.prompt.name": Tier.CLEAR_SAFE,
    "gen_ai.prompt.version": Tier.CLEAR_SAFE,
    # digest-only — provider-assigned correlation handles into provider logs.
    "gen_ai.response.id": Tier.DIGEST_ONLY,
    "gen_ai.request.previous_response.id": Tier.DIGEST_ONLY,
}

#: The draft's "never-enters" semconv rows, transcribed verbatim as their own
#: table — NOT consulted by the allow-list lookup (absence from
#: ``SEMCONV_ATTRS`` already denies them). Exists so a test can plant one of
#: these keys into a *copy* of ``SEMCONV_ATTRS`` and prove the real
#: block-builder leaks it, which the acceptance test requires. Never imported
#: by production code paths other than that test.
NEVER_ENTERS_SEMCONV: frozenset[str] = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.tool.definitions",
        "gen_ai.memory.records",
        "gen_ai.memory.query.text",
        "gen_ai.retrieval.query.text",
        "gen_ai.retrieval.documents",
        "gen_ai.conversation.id",
        "mcp.session.id",
        "session_id",
    }
)

#: Prefix form of the draft's ``gen_ai.prompt.variable.*``/``enduser.*``/
#: ``user.*`` never-enters rows (arbitrary suffixes, not enumerable keys).
NEVER_ENTERS_SEMCONV_PREFIXES: tuple[str, ...] = (
    "gen_ai.prompt.variable.",
    "enduser.",
    "user.",
)

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TRACE_FLAGS_RE = re.compile(r"^[0-9a-f]{2}$")


def tier_for_semconv_attribute(key: str, table: dict[str, Tier] = SEMCONV_ATTRS) -> Tier | None:
    """``None`` when *key* is not in *table* — default-deny, the only path
    :func:`capsule_emit.otel.block.build_otel_block` uses to decide whether a
    semconv attribute leaves the process at all. *table* is a parameter (not
    hardcoded to the module global) so the leak-mutant test can pass in a
    deliberately broken copy without monkeypatching module state.
    """
    return table.get(key)
