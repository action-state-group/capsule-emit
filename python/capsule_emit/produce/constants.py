# SPDX-License-Identifier: Apache-2.0
"""Protocol vocabulary for the producing API, mirroring Go ``constants.go``.

The value classes below are plain namespaces of strings, so a caller may pass
either ``ActionType.FYI`` or ``"fyi"``. Registry-backed vocabularies are
extensible; the values here are the ones AAC format 4 seeds.
"""
from __future__ import annotations

__all__ = [
    "SPEC_VERSION",
    "ACCEPTED_SPEC_VERSIONS",
    "FORMAT_VERSION",
    "CANONICALIZATION_ID",
    "CONTENT_TYPE",
    "ActionType",
    "Decision",
    "Approver",
    "VerdictClass",
    "EffectStatus",
    "IrreversibilityClass",
    "EffectAttestation",
    "ChainRelation",
    "EffectMode",
    "AttestationMode",
    "LedgerMode",
    "Domain",
    "Provenance",
    "ProvenanceModeValue",
    "TimeRung",
]

# These mirror agent-action-capsule's DEFAULT_SPEC_VERSION,
# ACCEPTED_SPEC_VERSIONS and CAPSULE_ID_MEDIA_TYPE; a test keeps them equal.

#: The spec_version every Capsule this package builds carries (draft -05).
SPEC_VERSION = "draft-mih-scitt-agent-action-capsule-05"
#: The revisions a verifier accepts for format 4. Informational only:
#: spec_version selects no algorithm, and verify_capsule never branches on it.
ACCEPTED_SPEC_VERSIONS = ("draft-mih-scitt-agent-action-capsule-04", SPEC_VERSION)
#: The AAC serialization-suite version.
FORMAT_VERSION = "4"
#: The only canonicalization profile emitted for format-4 Capsules.
CANONICALIZATION_ID = "jcs"
#: The Producer Envelope payload media type.
CONTENT_TYPE = "application/agent-action-capsule-id"


class ActionType:
    """Whether the Capsule is informational or records a decision."""

    FYI = "fyi"
    DECIDE = "decide"


class Decision:
    """How an action was disposed (extensible registry)."""

    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_INPUT = "needs_input"
    DEFERRED = "deferred"


class Approver:
    """Who disposed the action (closed enumeration)."""

    HUMAN = "human"
    POLICY = "policy"
    COUNTERPARTY = "counterparty"


class VerdictClass:
    """The terminal or open verdict (extensible registry)."""

    EXECUTED = "executed"
    BLOCKED = "blocked"
    HITL_DISPATCHED = "hitl_dispatched"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ERRORED = "errored"
    ENGINE_FAILURE = "engine_failure"
    DEFERRED = "deferred"
    NEEDS_DECISION = "needs_decision"
    EXPIRED = "expired"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    EPOCH_BOUNDARY = "epoch_boundary"


class EffectStatus:
    """How far an external effect progressed."""

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REVERTED = "reverted"


class IrreversibilityClass:
    """Effects ordered by consequence (extensible registry)."""

    TWO_WAY = "two_way"
    ONE_WAY_RECOVERABLE = "one_way_recoverable"
    ONE_WAY_CONSEQUENTIAL = "one_way_consequential"
    ONE_WAY_TERMINAL = "one_way_terminal"


class EffectAttestation:
    """Who vouches for execution (extensible registry)."""

    GATE_EXECUTED = "gate_executed"
    RUNTIME_CLAIMED = "runtime_claimed"


class ChainRelation:
    """How a Capsule relates to its parent (extensible registry)."""

    CONFIRMS = "confirms"
    SUPERSEDES = "supersedes"
    EPOCH_OPENS = "epoch_opens"
    #: A backfilled Capsule duplicating its contemporaneous twin (draft -05).
    DUPLICATES = "duplicates"


class EffectMode:
    """Effect assurance derived from an Effect."""

    NOT_APPLICABLE = "not_applicable"
    DISPATCHED_UNCONFIRMED = "dispatched_unconfirmed"
    CONFIRMED = "confirmed"


class AttestationMode:
    """Producer or transparency attestation tier."""

    SELF_ATTESTED = "self_attested"
    ANCHORED = "anchored"


class LedgerMode:
    """Custody tier derived from chain and receipt evidence."""

    STANDALONE = "standalone"
    CHAINED = "chained"
    ANCHORED = "anchored"


class Domain:
    """Epistemic role of the recorded action (extensible registry)."""

    ACTION = "action"
    MEMORY = "memory"
    REASONING = "reasoning"


class Provenance:
    """Producer tier (extensible registry)."""

    GATE = "gate"
    RUNTIME = "runtime"
    COLLECTOR = "collector"


class ProvenanceModeValue:
    """Contemporaneous observation or backfilled import (closed enumeration)."""

    CONTEMPORANEOUS = "contemporaneous"
    BACKFILLED = "backfilled"


class TimeRung:
    """Occurrence-time claim a backfilled Capsule may make (closed enumeration)."""

    SELF_ATTESTED = "self_attested"
    WITNESSED = "witnessed"
