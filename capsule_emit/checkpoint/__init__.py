# SPDX-License-Identifier: Apache-2.0
"""capsule_emit.checkpoint -- the CLL (Checkpointed Local Log) core.

An **opt-in** subpackage: nothing in ``capsule_emit``'s top-level import path
imports this package, so a caller who never does ``import
capsule_emit.checkpoint`` (or ``from capsule_emit.checkpoint import ...``)
pays zero cost for it -- no MMR module loaded, no extra dependency pulled in.

``core`` is the pure MMR algorithm (position math, domain-separated hashing,
inclusion/consistency proofs, no I/O). ``store`` is the v0 in-memory node
backing. ``index`` wires the MMR to any append-only log exposing the
``LogSource`` shape (append/scan/fetch/find_gaps/verify) -- structurally, not
by importing a concrete implementation. ``emit`` builds, signs, and registers
peaks checkpoints against a Transparency Service (the free public-good tier
at ``anchor.agentactioncapsule.org`` by default, any conforming TS
substitutable).

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
