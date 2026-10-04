# SPDX-License-Identifier: Apache-2.0
"""``export`` -- a SCOPED evidence file for a third party.

An evidence file (``evidence-bundle/v2``) is what ``capsule-emit report``
renders. Built from a whole ledger (``permalink --ledger --bundle-out``) it
hands the reader every record the node ever wrote, across every
counterparty, and with no signed checkpoint the page itself says the
selection cannot be proven. This module builds the file the other way
round: only the records in scope, each proven to sit in the node's
committed log.

**Scope** (exactly one): ``exchange`` (an exchange id), ``peer`` (a node id
named as the record's server or requester), ``since``/``until`` (a
timestamp window), ``record`` (one capsule id), or ``all`` -- the whole
ledger, only ever on request.

**Proof.** The ledger's leaves are rebuilt in order (every line, padding
included: padding is a leaf, never a record), checked against the newest
checkpoint whose own signature verifies, and each output file carries:

- the checkpoint, with its portable COSE form (``checkpoint.cose``), so a
  verifier authenticates it instead of calling it producer-asserted;
- a completeness certificate with one inclusion proof per record.

Each record is proven on its own: an inclusion proof against the
checkpoint's root. The file says ``selection: "producer-selected"`` and carries no
range proof, so it claims nothing about the records between the ones it
carries (a range proof can only prove a suffix of the log that ends at the
checkpoint, every record of it in the file -- the whole history again). A
record not yet covered by a checkpoint is refused, by name: it can be
exported once the next checkpoint covers it.

The ledger layout read here is a ledger directory holding
``capsules.jsonl`` (one sealed capsule per line, plus padding lines with
``record_type: "padding"``) and ``checkpoints.jsonl`` (``kind:
"mmr_checkpoint"`` lines), as the Mesh-LLM capsule-emit-mesh plugin writes
it, and as ``cll``'s checkpointed local log does.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .permalink import BUNDLE_KIND, BUNDLE_VERSION

PADDING = "padding"


class ExportError(ValueError):
    """The export was refused; the message says why."""


@dataclass(frozen=True)
class Scope:
    exchange: str | None = None
    peer: str | None = None
    since: str | None = None
    until: str | None = None
    record: str | None = None
    all: bool = False

    def check(self) -> None:
        given = [bool(self.exchange), bool(self.peer), bool(self.since or self.until), bool(self.record), self.all]
        if sum(given) != 1:
            raise ExportError(
                "name exactly one scope: --exchange ID, --peer NODE, --since/--until TIME, --record ID, "
                "or --all (the whole ledger, every counterparty)"
            )

    def describe(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


def _lines(path: Path) -> list[dict]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as err:
            raise ExportError(f"{path}:{n}: not a JSON line ({err})") from err
    return out


def _mesh_fields(capsule: dict) -> dict:
    compute = (capsule.get("model_attestation") or {}).get("compute_attestation") or {}
    poc = compute.get("x-mesh-poc-v1") or {}
    serving = poc.get("serving_provenance") or {}
    settlement = compute.get("x-mesh-settlement-v1") or {}
    return {
        "exchange_id": serving.get("exchange_id") or settlement.get("exchange_id"),
        "peers": {p for p in (serving.get("served_by_node_id"), serving.get("requesting_party")) if p},
    }


def _in_scope(capsule: dict, scope: Scope) -> bool:
    if scope.all:
        return True
    if scope.record:
        return capsule.get("capsule_id") == scope.record
    fields = _mesh_fields(capsule)
    if scope.exchange:
        return fields["exchange_id"] == scope.exchange
    if scope.peer:
        return scope.peer in fields["peers"]
    stamp = capsule.get("timestamp") or ""
    return (not scope.since or stamp >= scope.since) and (not scope.until or stamp <= scope.until)


def _checkpoint(ledger_dir: Path) -> Any:
    """The newest checkpoint whose own signature verifies."""
    from cll.checkpoint import CheckpointRecord, verify_checkpoint_signature_offline

    path = ledger_dir / "checkpoints.jsonl"
    if not path.exists():
        raise ExportError(f"{path}: no checkpoints yet -- nothing can be proven until the first checkpoint is cut")
    names = set(CheckpointRecord.__dataclass_fields__)
    good = []
    for raw in _lines(path):
        if raw.get("kind") != "mmr_checkpoint":
            continue
        cp = CheckpointRecord(**{k: v for k, v in raw.items() if k in names})
        if verify_checkpoint_signature_offline(cp):
            good.append(cp)
    if not good:
        raise ExportError(f"{path}: no checkpoint whose signature verifies")
    return max(good, key=lambda cp: cp.mmr_size)


class _MemoryLog:
    """The minimal log source ``cll``'s ``MmrLedger`` indexes: every leaf in order."""

    def __init__(self) -> None:
        self._n = 0

    def append(self, capsule: dict, *, consequential: bool = True) -> Any:  # noqa: ARG002
        self._n += 1
        return SimpleNamespace(seq=self._n, capsule_id=capsule["capsule_id"])

    def scan(self, *args: Any, **kwargs: Any) -> list:  # noqa: ARG002
        return []


def _mmr_at(entries: list[dict], cp: Any) -> Any:
    """The MMR rebuilt from the ledger's leaves up to ``cp``'s size, checked
    against ``cp``'s root."""
    from cll.checkpoint import MmrLedger

    mmr = MmrLedger(_MemoryLog())
    for entry in entries:
        if mmr.size() >= cp.mmr_size:
            break
        mmr.append({"capsule_id": entry["capsule_id"]})
    if mmr.size() != cp.mmr_size or mmr.root().hex() != cp.root:
        raise ExportError(
            f"the ledger does not match checkpoint mmr_size={cp.mmr_size}: its leaves rebuild to "
            f"size {mmr.size()}, root {mmr.root().hex()[:16]}..."
        )
    return mmr


def _proof_json(proof: Any) -> dict:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in proof.__dict__.items()}


def export(ledger_dir: str | Path, scope: Scope, *, signing_key: str | Path) -> dict:
    """Build the ``evidence-bundle/v2`` of the in-scope records.

    ``signing_key`` is the node's own Ed25519 key (PKCS#8 PEM) -- the key the
    checkpoint was signed with -- used only to produce the checkpoint's COSE
    form. It is never created here: a missing key is refused.
    """
    from cll.checkpoint import checkpoint_to_cose

    from .signing import LocalKeypairSigner

    scope.check()
    ledger_dir = Path(ledger_dir)
    entries = _lines(ledger_dir / "capsules.jsonl")
    key_path = Path(signing_key)
    if not key_path.is_file():
        raise ExportError(f"{key_path}: no signing key there (the export never creates one)")
    signer = LocalKeypairSigner(key_path)

    cp = _checkpoint(ledger_dir)
    if signer.key_id != cp.key_id:
        raise ExportError("the signing key is not the key that signed the checkpoint")
    mmr = _mmr_at(entries, cp)
    covered = mmr.leaf_count()

    selected = [
        (seq, e)
        for seq, e in enumerate(entries, 1)
        if e.get("record_type") != PADDING and _in_scope(e, scope)
    ]
    if not selected:
        raise ExportError(f"no record in scope {scope.describe()}")
    uncovered = [e["capsule_id"][:16] for seq, e in selected if seq > covered]
    if uncovered:
        raise ExportError(
            f"{len(uncovered)} record(s) in scope are not yet covered by a checkpoint "
            f"(covered: the first {covered} leaves): {', '.join(uncovered)} -- export again after the next checkpoint"
        )

    # A checkpoint that extends an earlier one carries the proof that it does
    # (the COSE form will not claim continuity without it).
    prev_peaks = mmr.peak_hashes_at(cp.prev_size) if cp.prev_size else None
    consistency = mmr.consistency_proof(cp.prev_size, cp.mmr_size) if cp.prev_size else None
    cose = checkpoint_to_cose(
        cp, signer, mmr.peak_hashes_at(cp.mmr_size), prev_peak_hashes=prev_peaks, consistency_proof=consistency
    )
    checkpoint = {
        "log_id": cp.log_id,
        "mmr_size": cp.mmr_size,
        "root": cp.root,
        "prev_size": cp.prev_size,
        "prev_root": cp.prev_root,
        "key_id": cp.key_id,
        "timestamp": cp.timestamp,
        "cose": base64.urlsafe_b64encode(cose).rstrip(b"=").decode(),
    }

    records = [e for _, e in selected]
    ids = {r["capsule_id"] for r in records}
    cited = sorted(
        {
            c
            for r in records
            for c in [(r.get("chain") or {}).get("parent_capsule_id")]
            + [ref.get("digest") for ref in r.get("references") or [] if isinstance(ref, dict)]
            if isinstance(c, str) and c and c not in ids
        }
    )
    memberships = {
        e["capsule_id"]: {
            "log_coordinates": {"log_id": cp.log_id, "seq": seq, "leaf_index": seq - 1},
            "inclusion_proof": _proof_json(mmr.inclusion_proof(seq, size=cp.mmr_size)),
        }
        for seq, e in selected
    }
    return {
        "bundle_version": BUNDLE_VERSION,
        "bundle_kind": BUNDLE_KIND,
        "root": records[-1]["capsule_id"],
        "records": records,
        "completeness": {
            "closure_depth": 0,
            "records_mode": "declared_incomplete" if cited else "complete",
            # The producer chose these records; nothing is claimed about the
            # records between them (see the module docstring).
            "selection": "producer-selected",
            "payloads_mode": "none",
            "suppressed_fields": [],
            "missing": cited,
        },
        "completeness_certificate": {
            "log_id": cp.log_id,
            "range_root": cp.root,
            "memberships": memberships,
        },
        "checkpoint": checkpoint,
        "extensions": {"x-scoped-export-v0": {"scope": scope.describe()}},
    }
