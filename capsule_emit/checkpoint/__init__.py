# SPDX-License-Identifier: Apache-2.0
"""capsule_emit.checkpoint -- thin compatibility re-export over ``cll.checkpoint``.

**Since 0.5.0, ``capsule_emit.core.emit()`` wires this in by DEFAULT** (see
``capsule_emit.witness``, and ``docs/checkpoint.md`` for the full story):
once a ledger accumulates enough entries to be checkpoint-worthy, a signed
peaks checkpoint over that ledger's MMR is built and registered with a
Transparency Service automatically, at its ``/checkpoints`` route -- async,
checkpoint-only (never capsule content), lazy, no caller code change
required. Disable per-call with ``emit(..., witness=False)`` or everywhere
with the ``CAPSULE_WITNESS=off`` env var.

This subpackage itself remains **structurally opt-in at import time**: that
default wiring is a thin layer above it (``capsule_emit.witness``) that
imports these modules lazily, only once a checkpoint is actually due --
nothing in ``capsule_emit``'s top-level import path (``import
capsule_emit`` alone, or a single below-cadence ``emit()`` call) loads an MMR
module or pulls in a network dependency. A caller who wants direct control
over cadence, signing keys, or a non-default Transparency Service can still
``import capsule_emit.checkpoint`` and drive ``MmrLedger`` /
``emit_checkpoint`` / ``register_checkpoint`` by hand; none of the functions
below ever makes a network call on its own (see ``checkpoint.emit``'s module
docstring).

**Graduated to the ``cll`` package (2026-09-01, W3.1 CLL extraction).** The
CLL/MMR core originally lived here (ported from
``capsule-ledger/capsule_ledger/mmr/{core,index,store}.py`` per Amendment E,
2026-08-21) on the reasoning that it is substrate a counterparty needs to
verify a log, so it should live where any consumer can depend on it without
forking. It has now graduated one level further, to the
``checkpointed-local-log`` repo's ``cll`` package -- the log layer is
deliberately NOT capsule-specific (that is its adoption story: e.g. a trace
registry running this exact mechanism over TRACE records), so it does not
live under a ``capsule-*``-branded name. This module is what stays: a thin,
behavior-preserving re-export so every existing
``capsule_emit.checkpoint.X`` / ``capsule_emit.checkpoint.<submodule>.X``
caller keeps working unchanged. ``core``/``cose_wire``/``emit``/``index``/
``store`` are each a one-line re-export of the matching ``cll.checkpoint``
submodule -- see those files.
"""
from .core import (
    ConsistencyProof,
    InclusionProof,
    IntegrityError,
    InvalidArgumentError,
    add_leaf,
    commitment_object,
    consistency_proof,
    height_at,
    interior_hash,
    leaf_count,
    leaf_hash,
    leaf_index_to_pos,
    node_count,
    peaks,
    pos_to_leaf_index,
    root_from_peaks,
    verify_consistency,
    verify_inclusion,
)
from .cose_wire import (
    CLL_CHECKPOINT_CONTENT_TYPE,
    WIRE_KIND,
    CoseCheckpointVerification,
    DecodedCheckpointCose,
    checkpoint_to_cose,
    encode_checkpoint_claims,
    verify_checkpoint_cose_offline,
)
from .emit import (
    DEFAULT_TS_PUBLIC_KEY_ID,
    DEFAULT_TS_PUBLIC_KEY_PEM,
    DEFAULT_TS_URL,
    EXAMPLE_CONFIG_TOML,
    STUB_MARKER,
    STUB_TS_URL,
    CheckpointConfig,
    CheckpointError,
    CheckpointRecord,
    Grade,
    RollbackError,
    Signer,
    StampVerdict,
    WitnessRecord,
    due_for_checkpoint,
    emit_checkpoint,
    lag_exceeded,
    register_checkpoint,
    register_checkpoint_stub,
    verify_checkpoint_consistency,
    verify_checkpoint_signature,
    verify_checkpoint_signature_offline,
    verify_receipt_offline,
    verify_witness_stamp_offline,
    verify_witness_stamp_tristate,
)
from .index import LogSource, MmrLedger, RangeProof, verify_range
from .store import MemoryNodeStore

__all__ = [
    "CLL_CHECKPOINT_CONTENT_TYPE",
    "WIRE_KIND",
    "CoseCheckpointVerification",
    "DecodedCheckpointCose",
    "checkpoint_to_cose",
    "encode_checkpoint_claims",
    "verify_checkpoint_cose_offline",
    "ConsistencyProof",
    "InclusionProof",
    "IntegrityError",
    "InvalidArgumentError",
    "add_leaf",
    "commitment_object",
    "consistency_proof",
    "height_at",
    "interior_hash",
    "leaf_count",
    "leaf_hash",
    "leaf_index_to_pos",
    "node_count",
    "peaks",
    "pos_to_leaf_index",
    "root_from_peaks",
    "verify_consistency",
    "verify_inclusion",
    "LogSource",
    "MmrLedger",
    "RangeProof",
    "verify_range",
    "MemoryNodeStore",
    "DEFAULT_TS_URL",
    "DEFAULT_TS_PUBLIC_KEY_PEM",
    "DEFAULT_TS_PUBLIC_KEY_ID",
    "EXAMPLE_CONFIG_TOML",
    "STUB_MARKER",
    "STUB_TS_URL",
    "CheckpointConfig",
    "CheckpointError",
    "CheckpointRecord",
    "Grade",
    "RollbackError",
    "Signer",
    "StampVerdict",
    "WitnessRecord",
    "due_for_checkpoint",
    "emit_checkpoint",
    "lag_exceeded",
    "register_checkpoint",
    "register_checkpoint_stub",
    "verify_checkpoint_consistency",
    "verify_checkpoint_signature",
    "verify_checkpoint_signature_offline",
    "verify_receipt_offline",
    "verify_witness_stamp_offline",
    "verify_witness_stamp_tristate",
]
