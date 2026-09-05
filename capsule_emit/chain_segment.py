# SPDX-License-Identifier: Apache-2.0
"""``chain_segment()`` — the cheap form of "history" (E14's third subject
kind, ``{kind: "chain_segment", from_size, to_size}`` or ``{last: N}``).

Without this, a stranger's ``range`` ask is one full :class:`~capsule_emit
.bundle.Bundle` per record — O(n) checkpoint copies, and the range cap makes
deep history unreachable. A ``chain_segment`` answers over the checkpoint
CHAIN instead of individual records: cp_a..cp_b — each signed checkpoint,
its witness receipts (already carried on ``CheckpointRecord.witnesses``),
and ONE consistency proof per link (``prev_size -> size``) — plus
per-checkpoint leaf counts by kind. No records, no inclusion proofs; size is
O(checkpoints), not O(records).

**Log vocabulary only.** This module ships exactly the vocabulary
``capsule-emit`` itself already owns: ``stamp`` (the log's own
checkpoint-stamp bookkeeping — see :mod:`capsule_emit.ledger`) and
``adjudication`` (:mod:`capsule_emit.adjudication`'s ``chain.relation ==
"adjudicates"``) — everything else classifies as the generic ``capsule``. A
caller with its own record taxonomy (e.g. a mesh deployment's
``exchange``/``card`` split) supplies its own ``classify`` callback; this
module never invents a third party's vocabulary itself.

**What the receiver can compute from it alone** (never shipped as fields
here — recomputed via :func:`verify_chain_segment`, the same
recompute+match discipline every other offline verifier in this repo
uses): ``history_depth`` (checkpoints verified), ``continuity`` at
checkpoint granularity (every ``prev_root`` links, every link's consistency
proof verifies), and ``witnessed: n/m`` (receipts verify offline, no
network). It cannot see a single record's content — that is ``record``/
``range``'s job, the drill-down once a chain segment names something worth
asking about.

**Adjudication verdict/role split.** For leaves this module classifies as
``adjudication`` (:mod:`capsule_emit.adjudication`'s three verdict shapes —
``corroborated``, ``inconclusive``, ``contradicted:<owner_id>``), each
checkpoint's counts are further split by ROLE relative to
``self_owner_id`` — the RESPONDING ledger's own signing ``key_id``, the
same identity every checkpoint and capsule on this ledger already carries
(see :mod:`capsule_emit.evidence_request`'s ``signer`` docs): a
``contradicted:<owner_id>`` verdict is ``contradicted_self`` when
``owner_id`` matches this ledger's own key, else ``contradicted_other`` —
never silently dropped, never assumed to be self without a match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from cll.checkpoint import core as mmr_core
from cll.checkpoint.emit import CheckpointRecord, verify_checkpoint_signature_offline
from cll.checkpoint.store import MemoryNodeStore

__all__ = [
    "ChainSegmentError",
    "CheckpointLink",
    "ChainSegment",
    "ChainSegmentVerifyResult",
    "chain_segment",
    "verify_chain_segment",
]


class ChainSegmentError(RuntimeError):
    """The requested segment cannot be built — an unknown boundary size, a
    size beyond the log's latest checkpoint, or no checkpoints at all.
    Caught by :func:`capsule_emit.evidence_request.answer` and turned into
    a ``coverage_unsatisfiable`` refusal — never a raw exception reaching a
    requester."""


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
    from cll.checkpoint.core import ConsistencyProof

    return ConsistencyProof(
        v=int(d["v"]),
        kind=d["kind"],
        size_a=int(d["size_a"]),
        size_b=int(d["size_b"]),
        old_peaks=tuple(d["old_peaks"]),
        witness=tuple(tuple(w) for w in d["witness"]),
        new_peaks=tuple(d["new_peaks"]),
    )


def _default_classify(entry: dict, *, kind_field: str, stamp_kind: str) -> str:
    """The only vocabulary this module ships on its own — see the module
    docstring's "log vocabulary only" note."""
    kind = entry.get(kind_field)
    if kind:
        return "stamp" if kind == stamp_kind else str(kind)
    chain = entry.get("chain")
    if isinstance(chain, dict) and chain.get("relation") == "adjudicates":
        return "adjudication"
    return "capsule"


def _adjudication_verdict(entry: dict) -> str | None:
    try:
        return entry["model_attestation"]["compute_attestation"]["adjudication"]["verdict"]
    except (KeyError, TypeError):
        return None


def _verdict_role_counts(entries: list[dict], *, self_owner_id: str | None) -> dict[str, int]:
    """``{corroborated, contradicted_self, contradicted_other,
    inconclusive}`` — see the module docstring's role-split note."""
    counts = {"corroborated": 0, "contradicted_self": 0, "contradicted_other": 0, "inconclusive": 0}
    for entry in entries:
        verdict = _adjudication_verdict(entry)
        if verdict is None:
            continue
        if verdict == "corroborated":
            counts["corroborated"] += 1
        elif verdict == "inconclusive":
            counts["inconclusive"] += 1
        elif verdict.startswith("contradicted:"):
            owner_id = verdict[len("contradicted:") :]
            if self_owner_id is not None and owner_id == self_owner_id:
                counts["contradicted_self"] += 1
            else:
                counts["contradicted_other"] += 1
    return counts


@dataclass(frozen=True)
class CheckpointLink:
    """One checkpoint in the segment, plus everything needed to verify it
    extends the PRIOR checkpoint IN THIS SEGMENT. ``consistency_proof`` is
    ``None`` only for the segment's own first checkpoint — the segment
    never proves anything about history before its own start boundary
    (that is the ``record``/``range`` bundle's ``prior_checkpoint`` role,
    not this one's)."""

    checkpoint: CheckpointRecord
    consistency_proof: Any | None  # cll.checkpoint.core.ConsistencyProof | None
    checkpoint_cose: bytes | None
    leaf_counts: dict[str, int]
    adjudication: dict[str, int] | None  # verdict/role split; present iff this checkpoint covers >=1 adjudication leaf
    leaf_digests: tuple[str, ...] | None  # None unless the request asked for leaf_digests=True

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "checkpoint": self.checkpoint.to_dict(),
            "checkpoint_cose": self.checkpoint_cose.hex() if self.checkpoint_cose is not None else None,
            "consistency_proof": (
                _consistency_proof_to_dict(self.consistency_proof) if self.consistency_proof is not None else None
            ),
            "leaf_counts": dict(self.leaf_counts),
        }
        if self.adjudication is not None:
            d["adjudication"] = dict(self.adjudication)
        if self.leaf_digests is not None:
            d["leaf_digests"] = list(self.leaf_digests)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CheckpointLink:
        cose_hex = d.get("checkpoint_cose")
        cproof = d.get("consistency_proof")
        return cls(
            checkpoint=CheckpointRecord.from_dict(d["checkpoint"]),
            consistency_proof=_consistency_proof_from_dict(cproof) if cproof is not None else None,
            checkpoint_cose=bytes.fromhex(cose_hex) if cose_hex else None,
            leaf_counts=dict(d.get("leaf_counts") or {}),
            adjudication=dict(d["adjudication"]) if "adjudication" in d else None,
            leaf_digests=tuple(d["leaf_digests"]) if "leaf_digests" in d else None,
        )


@dataclass(frozen=True)
class ChainSegment:
    """cp_a..cp_b — the checkpoint CHAIN artifact for E14's ``chain_segment``
    subject. See the module docstring for what it proves and what it
    deliberately does not (no records, no inclusion proofs)."""

    v: int
    links: tuple[CheckpointLink, ...]

    @property
    def checkpoint(self) -> CheckpointRecord:
        """The segment's LAST (``to``) checkpoint — what a requester's
        ``coverage.expected_pin``/``min_freshness`` pins against, the same
        convention :class:`capsule_emit.bundle.Bundle` uses for a single
        record. This lets :mod:`capsule_emit.evidence_request`'s existing
        pin/freshness resolution work UNCHANGED for this subject kind too."""
        return self.links[-1].checkpoint

    def to_dict(self) -> dict:
        return {"v": self.v, "links": [link.to_dict() for link in self.links]}

    @classmethod
    def from_dict(cls, d: dict) -> ChainSegment:
        return cls(v=int(d["v"]), links=tuple(CheckpointLink.from_dict(x) for x in d["links"]))


def chain_segment(
    entries: list[dict],
    *,
    from_size: int | None = None,
    to_size: int | None = None,
    last: int | None = None,
    self_owner_id: str | None = None,
    classify: Callable[[dict], str] | None = None,
    leaf_digests: bool = False,
    id_field: str = "capsule_id",
    kind_field: str = "kind",
    stamp_kind: str = "checkpoint_stamp",
) -> ChainSegment:
    """Build a standalone-verifiable :class:`ChainSegment` over the
    checkpoint chain already present in ``entries`` (the caller's own raw,
    in-append-order log entries — this module never reads a log itself; see
    ``capsule_emit.evidence_request._build_bundles`` for the file-reading
    caller).

    Selection is either ``last=N`` (the last ``N`` checkpoints — or fewer,
    honestly, if the log has not yet accumulated ``N``; a young log is
    never refused just because it hasn't reached the requested depth) or
    ``from_size``/``to_size`` (both required together, each must match an
    existing checkpoint's ``mmr_size`` exactly — ``from_size=0`` means "from
    the log's own genesis"). Raises :class:`ChainSegmentError` for a
    malformed selection, a ``to_size`` beyond the log's latest checkpoint
    ("ask beyond the log's size"), a boundary size that names no known
    checkpoint, or a log with no checkpoints at all.
    """
    if not entries:
        raise ChainSegmentError("entries: empty log")

    if last is not None:
        if from_size is not None or to_size is not None:
            raise ChainSegmentError("last is mutually exclusive with from_size/to_size")
        if not isinstance(last, int) or isinstance(last, bool) or last <= 0:
            raise ChainSegmentError(f"last must be a positive int, got {last!r}")
    else:
        if from_size is None or to_size is None:
            raise ChainSegmentError("chain_segment requires either last=N, or both from_size and to_size")
        if not isinstance(from_size, int) or isinstance(from_size, bool) or from_size < 0:
            raise ChainSegmentError(f"from_size must be a non-negative int, got {from_size!r}")
        if not isinstance(to_size, int) or isinstance(to_size, bool) or to_size < from_size:
            raise ChainSegmentError(f"to_size must be an int >= from_size, got {to_size!r}")

    stamp_entries = [e for e in entries if e.get(kind_field) == stamp_kind]
    checkpoints: list[CheckpointRecord] = [CheckpointRecord.from_dict(e["checkpoint"]) for e in stamp_entries]
    if not checkpoints:
        raise ChainSegmentError("no checkpoints recorded for this log yet")
    cose_by_size: dict[int, str] = {
        cp.mmr_size: e["checkpoint_cose"] for cp, e in zip(checkpoints, stamp_entries) if e.get("checkpoint_cose")
    }

    if last is not None:
        segment = checkpoints[-last:]
    else:
        by_size = {cp.mmr_size: cp for cp in checkpoints}
        latest = checkpoints[-1]
        if to_size not in by_size:
            if to_size > latest.mmr_size:
                raise ChainSegmentError(
                    f"to_size={to_size} is beyond this log's latest checkpoint (mmr_size={latest.mmr_size})"
                )
            raise ChainSegmentError(f"to_size={to_size} does not match any checkpoint this log has")
        end_idx = checkpoints.index(by_size[to_size])
        if from_size == 0:
            start_idx = 0
        else:
            if from_size not in by_size:
                raise ChainSegmentError(f"from_size={from_size} does not match any checkpoint this log has")
            start_idx = checkpoints.index(by_size[from_size])
            if start_idx > end_idx:
                raise ChainSegmentError(f"from_size={from_size} is after to_size={to_size}")
        segment = checkpoints[start_idx : end_idx + 1]

    classify_fn = classify or (lambda e: _default_classify(e, kind_field=kind_field, stamp_kind=stamp_kind))

    to_checkpoint = segment[-1]
    covered_leaves = mmr_core.leaf_count(to_checkpoint.mmr_size)
    store = MemoryNodeStore()
    for entry in entries[:covered_leaves]:
        body_digest = bytes.fromhex(entry[id_field])
        mmr_core.add_leaf(store, mmr_core.leaf_hash(body_digest))
    if store.size() != to_checkpoint.mmr_size:
        raise ChainSegmentError(
            f"reconstructed MMR size {store.size()} does not match checkpoint "
            f"mmr_size {to_checkpoint.mmr_size} — log may be corrupt or truncated"
        )

    links: list[CheckpointLink] = []
    prev_cp: CheckpointRecord | None = None
    for cp in segment:
        proof = None if prev_cp is None else mmr_core.consistency_proof(store, prev_cp.mmr_size, cp.mmr_size)

        own_leaf_start = mmr_core.leaf_count(cp.prev_size)
        own_leaf_end = mmr_core.leaf_count(cp.mmr_size)
        own_entries = entries[own_leaf_start:own_leaf_end]

        leaf_counts: dict[str, int] = {}
        adjudication_entries: list[dict] = []
        for e in own_entries:
            k = classify_fn(e)
            leaf_counts[k] = leaf_counts.get(k, 0) + 1
            if k == "adjudication":
                adjudication_entries.append(e)

        adjudication = (
            _verdict_role_counts(adjudication_entries, self_owner_id=self_owner_id) if adjudication_entries else None
        )

        links.append(
            CheckpointLink(
                checkpoint=cp,
                consistency_proof=proof,
                checkpoint_cose=(
                    bytes.fromhex(cose_by_size[cp.mmr_size]) if cose_by_size.get(cp.mmr_size) else None
                ),
                leaf_counts=leaf_counts,
                adjudication=adjudication,
                leaf_digests=(tuple(e[id_field] for e in own_entries) if leaf_digests else None),
            )
        )
        prev_cp = cp

    return ChainSegment(v=1, links=tuple(links))


@dataclass
class ChainSegmentVerifyResult:
    """Total, offline outcome of :func:`verify_chain_segment` — never
    raises. ``continuity``/``history_depth``/``witnessed`` are exactly the
    three properties a receiver renders per E14's acceptance
    ("depth/continuity/witnessed")."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    continuity: str = "unbroken"
    history_depth: int = 0
    witnessed: str = "0/0"


def verify_chain_segment(
    segment: ChainSegment, *, trust_anchor: dict[str, bytes | str] | None = None
) -> ChainSegmentVerifyResult:
    """Pure, offline, total verification of a standalone :class:`ChainSegment`
    — no reader, no network, never raises. Walks the segment's own links,
    re-verifying every checkpoint's signature and every consistency proof
    from the artifact alone (no live MMR needed — the whole point of
    carrying ``consistency_proof`` on each link). Stops at the FIRST broken
    link — a break never "silently re-links" past the gap — and labels
    ``continuity`` with exactly where and why.
    """
    from cll.checkpoint.emit import StampVerdict, verify_witness_stamp_tristate

    if not segment.links:
        return ChainSegmentVerifyResult(
            ok=False, errors=["empty segment -- nothing to verify"], continuity="no checkpoints", history_depth=0, witnessed="0/0"
        )

    errors: list[str] = []
    continuity = "unbroken"
    depth = 0
    witnessed_count = 0
    prev: CheckpointRecord | None = None

    for link in segment.links:
        cp = link.checkpoint
        if not verify_checkpoint_signature_offline(cp):
            msg = f"broken at mmr_size={cp.mmr_size}: checkpoint signature does not verify"
            errors.append(msg)
            continuity = msg
            break

        if prev is not None:
            if cp.prev_size != prev.mmr_size or cp.prev_root != prev.root:
                msg = (
                    f"broken at mmr_size={cp.mmr_size}: does not chain from the prior checkpoint "
                    f"in this segment (mmr_size={prev.mmr_size})"
                )
                errors.append(msg)
                continuity = msg
                break
            if link.consistency_proof is None:
                msg = f"broken at mmr_size={cp.mmr_size}: missing consistency proof for a non-first link"
                errors.append(msg)
                continuity = msg
                break
            root_a = bytes.fromhex(prev.root)
            root_b = bytes.fromhex(cp.root)
            if not mmr_core.verify_consistency(root_a, prev.mmr_size, root_b, cp.mmr_size, link.consistency_proof):
                msg = (
                    f"broken at mmr_size={cp.mmr_size}: consistency proof does not bridge "
                    f"{prev.mmr_size} -> {cp.mmr_size}"
                )
                errors.append(msg)
                continuity = msg
                break

        depth += 1
        if cp.witnesses:
            verdicts = [
                verify_witness_stamp_tristate(cp, w, ts_pubkey_pem=(trust_anchor or {}).get(w.ts_url))[0]
                for w in cp.witnesses
            ]
            if any(v is StampVerdict.WITNESSED for v in verdicts):
                witnessed_count += 1
        prev = cp

    return ChainSegmentVerifyResult(
        ok=not errors,
        errors=errors,
        continuity=continuity,
        history_depth=depth,
        witnessed=f"{witnessed_count}/{len(segment.links)}",
    )
