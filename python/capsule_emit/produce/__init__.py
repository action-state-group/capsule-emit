# SPDX-License-Identifier: Apache-2.0
"""The storage-free producing API, matching the Go and TypeScript packages.

It builds Agent Action Capsule format-4 records from values an application
observed, signs their Producer Envelopes and verifies both, and writes
nothing: storage and witnessing stay with the caller. For the
same inputs it returns byte-identical Capsule and envelope bytes to
``github.com/action-state-group/capsule-emit/go`` and
``@action-state-group/capsule-emit``.

::

    JSON values -> digest_json -> agent input/output digests -> build -> Capsule
    foreign bytes -> carry / received -> Capsule
    existing Capsules -> who / can / did / audit -> build_composition -> Capsule
    Capsule + identity -> sign -> Producer Envelope
    SealInput -> digest_json + build/build_composition + sign -> Capsule + envelope

Canonicalization, Capsule ID, Class 1 and envelope verification are
agent-action-capsule's. Only ``fyi`` and ``decide`` action types are built.
"""
from __future__ import annotations

from .build import build, format_timestamp
from .constants import (
    ACCEPTED_SPEC_VERSIONS,
    CANONICALIZATION_ID,
    CONTENT_TYPE,
    FORMAT_VERSION,
    SPEC_VERSION,
    ActionType,
    Approver,
    AttestationMode,
    ChainRelation,
    Decision,
    Domain,
    EffectAttestation,
    EffectMode,
    EffectStatus,
    IrreversibilityClass,
    LedgerMode,
    Provenance,
    ProvenanceModeValue,
    TimeRung,
    VerdictClass,
)
from .construction import audit, build_composition, can, carry, did, received, who
from .digest import digest_json
from .model import (
    JSON_NULL,
    BuiltPayload,
    Chain,
    ComputeAttestation,
    Disposition,
    Effect,
    Extension,
    Input,
    Model,
    ProvenanceMode,
    Reference,
    Result,
    SealInput,
    SlotMember,
)
from .sign import SigningIdentity, generate_signing_identity, new_ed25519_signing_identity, seal, sign
from .verify import Class1Error, EnvelopeVerificationError, decode_payload, verify_capsule, verify_envelope

__all__ = [
    # functions
    "digest_json",
    "build",
    "received",
    "carry",
    "who",
    "can",
    "did",
    "audit",
    "build_composition",
    "sign",
    "seal",
    "verify_envelope",
    "verify_capsule",
    "decode_payload",
    "format_timestamp",
    "new_ed25519_signing_identity",
    "generate_signing_identity",
    # types
    "Input",
    "SealInput",
    "Model",
    "ComputeAttestation",
    "Disposition",
    "Effect",
    "Chain",
    "Reference",
    "ProvenanceMode",
    "Extension",
    "BuiltPayload",
    "Result",
    "SlotMember",
    "SigningIdentity",
    "JSON_NULL",
    "Class1Error",
    "EnvelopeVerificationError",
    # constants
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
