# SPDX-License-Identifier: Apache-2.0
"""``bundle()`` — the hand-to-anyone artifact (O16 audit item 14, frozen
surface §2.5).

The verification chain documented in ``capsule_emit.checkpoint.emit`` is
four separate, caller-composed primitives: MMR inclusion, checkpoint
signature, TS receipt, and rollback/consistency. This module is what
assembles them into ONE standalone object for one record, per §2.5:

    {receipt, inclusion proof, covering checkpoint (+ its witness stamp),
     prior checkpoint, consistency proof between the two}

Once built, a ``Bundle`` is offline-verifiable by a stranger — no account,
no further help from the producer, no network (see :func:`verify_bundle`;
witness-stamp re-confirmation is a separate, explicitly optional step since
it may need a network fetch of the Transparency Service's public key). It
gives the two-sided append bracket the frozen surface names (§2.4): the
record provably entered the log no later than the covering checkpoint's
stamp and no earlier than the prior checkpoint (it wasn't in that one yet)
— except for a record covered by the very first checkpoint a log ever had,
where there is no prior checkpoint to bound the lower side (``prior_checkpoint``
and ``consistency_proof`` are both ``None``; ``checkpoint.prev_size == 0``
says so honestly rather than gap-filling one).

A bundle is buildable at any later time for any record the log still
retains — this module never caches or persists one; every call re-reads the
ledger and re-derives the MMR fresh from it, exactly the way
``capsule_emit.witness`` built it at production time (each raw ledger line —
capsule or checkpoint-stamp alike — is one leaf, in append order; see
``capsule_emit.ledger``'s module docstring).

Deliberately NOT imported from ``capsule_emit/__init__.py`` — like
``capsule_emit.checkpoint`` and ``capsule_emit.status``, this stays
structurally opt-in (``from capsule_emit.bundle import bundle``) so a bare
``import capsule_emit`` never pays for the MMR/checkpoint subpackage (see
``tests/test_checkpoint_layer0_cost.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Bundle", "BundleError", "bundle", "verify_bundle"]


class BundleError(RuntimeError):
    """A bundle cannot be built for the requested record — not found,
    ambiguous, or not yet covered by any checkpoint."""


@dataclass(frozen=True)
class Bundle:
    """A standalone-verifiable evidence package for one ledger record.

    ``prior_checkpoint``/``consistency_proof`` are ``None`` together, iff
    ``checkpoint.prev_size == 0`` (the covering checkpoint is the log's
    first) — never independently ``None``.
    """

    v: int
    capsule_id: str
    seq: int  # 1-indexed position in the raw ledger (capsules + stamps)
    receipt: dict
    inclusion_proof: Any  # capsule_emit.checkpoint.core.InclusionProof
    checkpoint: Any  # capsule_emit.checkpoint.CheckpointRecord — covering, carries its stamp(s)
    prior_checkpoint: Any | None  # capsule_emit.checkpoint.CheckpointRecord | None
    consistency_proof: Any | None  # capsule_emit.checkpoint.core.ConsistencyProof | None

    def to_dict(self) -> dict:
        return {
            "v": self.v,
            "capsule_id": self.capsule_id,
            "seq": self.seq,
            "receipt": self.receipt,
            "inclusion_proof": _inclusion_proof_to_dict(self.inclusion_proof),
            "checkpoint": self.checkpoint.to_dict(),
            "prior_checkpoint": self.prior_checkpoint.to_dict() if self.prior_checkpoint else None,
            "consistency_proof": (
                _consistency_proof_to_dict(self.consistency_proof)
                if self.consistency_proof is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Bundle:
        from .checkpoint.emit import CheckpointRecord

        prior = d.get("prior_checkpoint")
        cproof = d.get("consistency_proof")
        return cls(
            v=int(d["v"]),
            capsule_id=d["capsule_id"],
            seq=int(d["seq"]),
            receipt=d["receipt"],
            inclusion_proof=_inclusion_proof_from_dict(d["inclusion_proof"]),
            checkpoint=CheckpointRecord.from_dict(d["checkpoint"]),
            prior_checkpoint=CheckpointRecord.from_dict(prior) if prior else None,
            consistency_proof=_consistency_proof_from_dict(cproof) if cproof is not None else None,
        )


def _inclusion_proof_to_dict(p: Any) -> dict:
    return {
        "v": p.v,
        "kind": p.kind,
        "size": p.size,
        "leaf_index": p.leaf_index,
        "witness": list(p.witness),
        "peaks_left": list(p.peaks_left),
        "peaks_right": list(p.peaks_right),
    }


def _inclusion_proof_from_dict(d: dict) -> Any:
    from .checkpoint.core import InclusionProof

    return InclusionProof(
        v=int(d["v"]),
        kind=d["kind"],
        size=int(d["size"]),
        leaf_index=int(d["leaf_index"]),
        witness=tuple(d["witness"]),
        peaks_left=tuple(d["peaks_left"]),
        peaks_right=tuple(d["peaks_right"]),
    )


def _consistency_proof_to_dict(p: Any) -> dict:
    return {
        "v": p.v,
        "kind": p.kind,
        "size_a": p.size_a,
        "size_b": p.size_b,
        "old_peaks": list(p.old_peaks),
        "witness": [list(w) for w in p.witness],
        "new_peaks": list(p.new_peaks),
    }


def _consistency_proof_from_dict(d: dict) -> Any:
    from .checkpoint.core import ConsistencyProof

    return ConsistencyProof(
        v=int(d["v"]),
        kind=d["kind"],
        size_a=int(d["size_a"]),
        size_b=int(d["size_b"]),
        old_peaks=tuple(d["old_peaks"]),
        witness=tuple(tuple(w) for w in d["witness"]),
        new_peaks=tuple(d["new_peaks"]),
    )


def _find_record(entries: list[dict], capsule_id: str) -> int:
    """Resolve ``capsule_id`` (full, or an unambiguous >=8-char prefix — same
    convention as ``ledger.show``/CLI ``--reveal``) to its 0-based index in
    ``entries``. Checkpoint-stamp entries are never a match — a bundle is
    for a record (capsule), never for the log's own bookkeeping."""
    from .ledger import CHECKPOINT_STAMP_KIND

    matches = [
        i
        for i, e in enumerate(entries)
        if e.get("kind") != CHECKPOINT_STAMP_KIND
        and (
            e.get("capsule_id") == capsule_id
            or (len(capsule_id) >= 8 and str(e.get("capsule_id", "")).startswith(capsule_id))
        )
    ]
    if not matches:
        raise BundleError(f"no record matches capsule_id {capsule_id!r}")
    exact = [i for i in matches if entries[i]["capsule_id"] == capsule_id]
    if exact:
        return exact[0]
    if len(matches) > 1:
        raise BundleError(
            f"capsule_id prefix {capsule_id!r} matches {len(matches)} records — use more characters"
        )
    return matches[0]


def bundle(path: Any, capsule_id: str) -> Bundle:
    """Build a standalone-verifiable :class:`Bundle` for one record in the
    JSONL ledger at ``path``.

    Re-reads the whole ledger and re-derives the MMR fresh each call — this
    never assumes an in-process ``MmrLedger`` is warm (bundle can be built by
    a completely different process than the one that sealed the record).

    Raises :class:`BundleError` if ``capsule_id`` doesn't resolve to exactly
    one record, or if that record is not yet covered by any checkpoint (a
    record only becomes bundle-able once a checkpoint's ``mmr_size`` reaches
    it — see ``capsule_emit.status`` for a read-only way to check that lag
    before calling this).
    """
    from .checkpoint import core as mmr_core
    from .checkpoint.emit import CheckpointRecord
    from .checkpoint.store import MemoryNodeStore
    from .ledger import CHECKPOINT_STAMP_KIND, read_ledger_entries

    entries = read_ledger_entries(path)
    if not entries:
        raise BundleError(f"{path}: empty or not found")

    target_idx = _find_record(entries, capsule_id)
    seq = target_idx + 1  # 1-indexed leaf position, matching production's raw-line numbering

    checkpoints: list[CheckpointRecord] = [
        CheckpointRecord.from_dict(e["checkpoint"])
        for e in entries
        if e.get("kind") == CHECKPOINT_STAMP_KIND
    ]

    covering = next((cp for cp in checkpoints if mmr_core.leaf_count(cp.mmr_size) >= seq), None)
    if covering is None:
        raise BundleError(
            f"record {capsule_id!r} (seq={seq}) is not yet covered by any checkpoint — "
            "no bundle exists yet; it becomes buildable once the next checkpoint covers it"
        )

    prior = None
    if covering.prev_size > 0:
        prior = next((cp for cp in checkpoints if cp.mmr_size == covering.prev_size), None)
        if prior is None:
            raise BundleError(
                f"checkpoint at mmr_size={covering.mmr_size} names a prior checkpoint at "
                f"mmr_size={covering.prev_size} that is not present in {path} — ledger is incomplete"
            )

    covered_leaves = mmr_core.leaf_count(covering.mmr_size)
    store = MemoryNodeStore()
    for entry in entries[:covered_leaves]:
        body_digest = bytes.fromhex(entry["capsule_id"])
        mmr_core.add_leaf(store, mmr_core.leaf_hash(body_digest))
    if store.size() != covering.mmr_size:
        raise BundleError(
            f"reconstructed MMR size {store.size()} does not match checkpoint "
            f"mmr_size {covering.mmr_size} for {path} — ledger may be corrupt or truncated"
        )

    inclusion = mmr_core.inclusion_proof(store, seq - 1, covering.mmr_size)
    consistency = (
        mmr_core.consistency_proof(store, prior.mmr_size, covering.mmr_size) if prior is not None else None
    )

    return Bundle(
        v=1,
        capsule_id=entries[target_idx]["capsule_id"],
        seq=seq,
        receipt=entries[target_idx],
        inclusion_proof=inclusion,
        checkpoint=covering,
        prior_checkpoint=prior,
        consistency_proof=consistency,
    )


def verify_bundle(b: Bundle) -> tuple[bool, list[str]]:
    """Pure, offline, total verification of a standalone :class:`Bundle` —
    no reader, no network, never raises. Confirms every link the two-sided
    append bracket depends on:

      1. the receipt's own ``capsule_id`` matches the leaf the inclusion
         proof was built for;
      2. inclusion — the receipt is genuinely a leaf under the covering
         checkpoint's root, at this bundle's ``seq``;
      3. the covering checkpoint's own signature, offline
         (``capsule_emit.checkpoint.verify_checkpoint_signature_offline`` —
         Ed25519, via the checkpoint's own ``key_id``, no private key
         needed);
      4. if a prior checkpoint is present: its signature too, that the
         covering checkpoint's ``prev_size``/``prev_root`` genuinely name
         it, and the consistency proof bridging the two roots; if absent:
         that ``checkpoint.prev_size == 0`` — this is honestly the log's
         first checkpoint, not a silently dropped lower bound.

    Deliberately does NOT re-confirm witness stamps
    (``b.checkpoint.witnesses`` / ``b.prior_checkpoint.witnesses``) — that
    is a separate, optional step
    (``capsule_emit.checkpoint.verify_receipt_offline`` per
    ``WitnessRecord``), since it may need a network fetch of the
    Transparency Service's public key; a caller holding a cached
    ``ts_pubkey_pem`` can do that step fully offline too, just not as part
    of this pure check.

    Returns ``(ok, errors)`` — ``errors`` is empty iff ``ok``.
    """
    from .checkpoint import core as mmr_core
    from .checkpoint.emit import verify_checkpoint_signature_offline

    errors: list[str] = []
    try:
        if b.receipt.get("capsule_id") != b.capsule_id:
            errors.append("receipt.capsule_id does not match bundle.capsule_id")

        body_digest = bytes.fromhex(b.capsule_id)
        root = bytes.fromhex(b.checkpoint.root)
        if not mmr_core.verify_inclusion(
            root, b.checkpoint.mmr_size, b.seq - 1, body_digest, b.inclusion_proof
        ):
            errors.append("inclusion proof does not verify against the covering checkpoint's root")

        if not verify_checkpoint_signature_offline(b.checkpoint):
            errors.append("covering checkpoint signature does not verify")

        if b.prior_checkpoint is not None:
            if not verify_checkpoint_signature_offline(b.prior_checkpoint):
                errors.append("prior checkpoint signature does not verify")
            if b.checkpoint.prev_size != b.prior_checkpoint.mmr_size:
                errors.append("checkpoint.prev_size does not match prior_checkpoint.mmr_size")
            if b.checkpoint.prev_root != b.prior_checkpoint.root:
                errors.append("checkpoint.prev_root does not match prior_checkpoint.root")
            if b.consistency_proof is None:
                errors.append("prior_checkpoint is present but consistency_proof is missing")
            else:
                root_a = bytes.fromhex(b.prior_checkpoint.root)
                if not mmr_core.verify_consistency(
                    root_a,
                    b.prior_checkpoint.mmr_size,
                    root,
                    b.checkpoint.mmr_size,
                    b.consistency_proof,
                ):
                    errors.append("consistency proof does not bridge prior_checkpoint to checkpoint")
        else:
            if b.checkpoint.prev_size != 0:
                errors.append("prior_checkpoint is missing but checkpoint.prev_size != 0")
            if b.consistency_proof is not None:
                errors.append("consistency_proof present without a prior_checkpoint")
    except Exception as exc:  # noqa: BLE001 — pure verifier, never raises
        errors.append(f"unexpected error: {exc}")
        return False, errors

    return not errors, errors
