# SPDX-License-Identifier: Apache-2.0
"""Typed inputs and outputs of the producing API, mirroring Go ``model.go``.

Every type is a frozen dataclass. String members default to ``""`` and mean
"absent" when empty, exactly as Go's zero values do, so one input yields the
same Capsule bytes in Go, TypeScript and Python.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sign import SigningIdentity

__all__ = [
    "Model",
    "ComputeAttestation",
    "Disposition",
    "Effect",
    "Chain",
    "Reference",
    "ProvenanceMode",
    "Extension",
    "Input",
    "BuiltPayload",
    "Result",
    "SlotMember",
    "SealInput",
    "JSON_NULL",
]


class _JSONNull:
    """Sentinel committing an explicit JSON ``null`` in :class:`SealInput`."""

    _instance: _JSONNull | None = None

    def __new__(cls) -> _JSONNull:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "JSON_NULL"


#: Pass as ``SealInput.payload`` or ``SealInput.agent_output`` to digest-commit
#: JSON ``null``; ``None`` means the value is absent (Go's nil).
JSON_NULL = _JSONNull()


@dataclass(frozen=True)
class Model:
    """The provider and model that performed the recorded work."""

    provider: str = ""
    model_id: str = ""


@dataclass(frozen=True)
class ComputeAttestation:
    """Binds the model invocation to its input, output and runtime. Digests
    are AAC JSON-DIGEST values produced by ``digest_json``."""

    agent_input_digest: str = ""
    agent_output_digest: str = ""
    runtime: str = ""


@dataclass(frozen=True)
class Disposition:
    """How a decision was disposed."""

    decision: str
    approver: str
    human_disposed: bool = False
    verdict_class: str = ""
    reason_digest: str = ""


@dataclass(frozen=True)
class Effect:
    """An external side effect, binding request and response JSON by digest."""

    type: str
    status: str
    irreversibility_class: str
    effect_attestation: str = ""
    request_digest: str = ""
    response_digest: str = ""
    external_ref: str = ""


@dataclass(frozen=True)
class Chain:
    """Links a Capsule to one parent Capsule."""

    parent_capsule_id: str
    relation: str


@dataclass(frozen=True)
class Reference:
    """Cites an external artifact under its own CPB digest context.
    ``log_coordinates`` is an opaque recorded claim; nothing verifies it."""

    type: str
    digest_alg: str
    digest: str
    citation_purpose: str = ""
    log_coordinates: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProvenanceMode:
    """Marks a Capsule as a backfilled import (draft -05 §5.3(bis)). Mode
    ``backfilled`` requires the four companion fields; ``time_rung`` is
    meaningful only then."""

    mode: str
    source_ref: Reference | None = None
    source_asserted_at: str = ""
    import_batch: str = ""
    imported_at: str = ""
    time_rung: str = ""


@dataclass(frozen=True)
class Extension:
    """One additional top-level member a companion document defines.
    ``value`` is raw JSON text (``bytes`` or ``str``), decoded strictly; it
    must be float-free and not ``null``."""

    name: str
    value: bytes | str


@dataclass(frozen=True)
class Input:
    """Application-owned values for one Capsule. ``action_id`` and
    ``timestamp`` are explicit: the library never invents business identity
    or wall-clock time. ``timestamp`` is a timezone-aware ``datetime`` or an
    RFC 3339 string with an offset; it is normalized to UTC microseconds."""

    action_id: str
    action_type: str
    operator: str
    developer: str
    timestamp: datetime | str
    epoch_id: str = ""
    domain: str = ""
    provenance: str = ""
    disposition: Disposition | None = None
    effect: Effect | None = None
    chain: Chain | None = None
    references: Sequence[Reference] | None = None
    model: Model | None = None
    compute: ComputeAttestation | None = None
    provenance_mode: ProvenanceMode | None = None
    extensions: Sequence[Extension] = ()


@dataclass(frozen=True)
class BuiltPayload:
    """A validated signature-free format-4 Capsule: its ID, decoded value and
    exact JCS bytes."""

    capsule_id: str
    value: Mapping[str, Any] = field(repr=False)
    json: bytes = field(repr=False)


@dataclass(frozen=True)
class Result:
    """A signature-free Capsule and one Producer Envelope."""

    capsule_id: str
    payload: bytes = field(repr=False)
    envelope: bytes = field(repr=False)


@dataclass(frozen=True)
class SlotMember:
    """An existing Capsule bound to one composition role. Obtain one from
    ``who``, ``can``, ``did`` or ``audit``."""

    slot: str
    capsule_id: str
    json: bytes = field(repr=False)


@dataclass(frozen=True)
class SealInput:
    """The one-call producer input. For a regular Capsule, ``payload`` and
    ``agent_output`` are digest-committed as agent input and output digests
    unless ``None`` (use :data:`JSON_NULL` to commit ``null``). For a
    composition, set ``members``; ``payload`` and ``agent_output`` must then
    be ``None``."""

    capsule: Input
    identity: SigningIdentity
    payload: Any = None
    agent_output: Any = None
    model: Model | None = None
    runtime: str = ""
    members: Sequence[SlotMember] | None = None
