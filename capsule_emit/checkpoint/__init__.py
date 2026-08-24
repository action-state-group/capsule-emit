# SPDX-License-Identifier: Apache-2.0
"""capsule_emit.checkpoint -- the CLL (Checkpointed Local Log) core.

**Since 0.5.0, ``capsule_emit.core.emit()`` wires this in by DEFAULT** (see
``capsule_emit.witness``, and ``docs/checkpoint.md`` for the full story):
once a ledger accumulates enough entries to be checkpoint-worthy, a signed
peaks checkpoint over that ledger's MMR is built and registered with a
Transparency Service automatically -- async, digest-only, lazy, no caller
code change required. Disable per-call with ``emit(..., witness=False)`` or
everywhere with the ``CAPSULE_WITNESS=off`` env var.

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

``core`` is the pure MMR algorithm (position math, domain-separated hashing,
inclusion/consistency proofs, no I/O). ``store`` is the v0 in-memory node
backing. ``index`` wires the MMR to any append-only log exposing the
``LogSource`` shape (append/scan/fetch/find_gaps/verify) -- structurally, not
by importing a concrete implementation. ``emit`` builds, signs, and registers
peaks checkpoints against a Transparency Service (the free public-good tier
at ``witness.agentactioncapsule.org`` by default -- currently served via
``anchor.agentactioncapsule.org`` while its CNAME is pending, see
``emit._PENDING_CNAME_TARGETS`` -- any conforming TS substitutable, and more
than one may be registered at once for a multi-witness stream, see
``capsule_emit.witness``).

Ported from ``capsule-ledger/capsule_ledger/mmr/{core,index,store}.py`` per
Amendment E (2026-08-21): the CLL core is substrate a counterparty needs to
verify a log, so it lives here rather than forked per-consumer.
``capsule-ledger`` consumes this package through its public interface.
"""
from .core import (
    ConsistencyProof,
    InclusionProof,
    IntegrityError,
    InvalidArgumentError,
    add_leaf,
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
from .emit import (
    DEFAULT_TS_URL,
    EXAMPLE_CONFIG_TOML,
    CheckpointConfig,
    CheckpointError,
    CheckpointRecord,
    Grade,
    RollbackError,
    Signer,
    WitnessRecord,
    due_for_checkpoint,
    emit_checkpoint,
    lag_exceeded,
    register_checkpoint,
    verify_checkpoint_consistency,
    verify_checkpoint_signature,
    verify_receipt_offline,
)
from .index import LogSource, MmrLedger, RangeProof, verify_range
from .store import MemoryNodeStore

__all__ = [
    "ConsistencyProof",
    "InclusionProof",
    "IntegrityError",
    "InvalidArgumentError",
    "add_leaf",
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
    "EXAMPLE_CONFIG_TOML",
    "CheckpointConfig",
    "CheckpointError",
    "CheckpointRecord",
    "Grade",
    "RollbackError",
    "Signer",
    "WitnessRecord",
    "due_for_checkpoint",
    "emit_checkpoint",
    "lag_exceeded",
    "register_checkpoint",
    "verify_checkpoint_consistency",
    "verify_checkpoint_signature",
    "verify_receipt_offline",
]
