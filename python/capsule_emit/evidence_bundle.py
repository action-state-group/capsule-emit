# SPDX-License-Identifier: Apache-2.0
"""Proof sources for Evidence Bundles (``evidence-bundle/v2``) over the logs
capsule-emit writes that are not CLL backends of their own.

The producer is agent-action-capsule's: ``agent_action_capsule.bundle.
build_evidence_bundle(source, root=..., interval_from=...)`` reads a proof
source, verifies everything it reads against the signed checkpoint, and checks
its result with the strict verifier before returning it. A CLL backend (entry
log plus checkpoint state) and a record store become a proof source with
``agent_action_capsule.bundle.proof_source_from_cll(backend, records)``. This
module holds no producer; it puts the remaining ledger layout behind those two
interfaces:

- ``ledger.jsonl``: :class:`capsule_emit.legacy_ledger.LegacyLedger` already is
  both (``load_cll`` reads its checkpoint stamps), so the source is
  ``proof_source_from_cll(ledger, ledger)``.
- a mesh ledger directory (``capsules.jsonl`` and ``checkpoints.jsonl``, as
  the Mesh-LLM capsule-emit-mesh plugin and Rust ``capsule-emit`` write it):
  :class:`MeshLedgerLog`, used the same way.

A segmented ``cll.ledger.store.LedgerStore`` directory (``manifest.json`` and
``segments/``), which only the frozen Python mesh harness writes, is not read:
``capsule-emit bundle`` refuses it and names the flat layout it reads.

``capsule-emit bundle LEDGER`` (``capsule_emit.cli``) picks the adapter and
writes the bundle's JCS bytes.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cll.log import CllState, Entry
from cll.log import CorruptError as LogCorruptError
from cll.log.engine import check_scan

from .artifact.record import InvalidError, NotFoundError, Record, valid_id
from .legacy_ledger import _EPOCH, _cll_state_at, _line_time, checked_record

__all__ = ["MeshLedgerError", "MeshLedgerLog"]

_CHECKPOINT_KIND = "mmr_checkpoint"
_PADDING = "padding"


class MeshLedgerError(ValueError):
    """A mesh ledger directory cannot back a proven bundle; the message says
    why."""


def _json_lines(path: Path) -> Iterator[tuple[int, dict]]:
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LogCorruptError(f"{path}:{number}: not a JSON line ({exc})") from exc
        if not isinstance(obj, dict):
            raise LogCorruptError(f"{path}:{number}: not a JSON object")
        yield number, obj


class MeshLedgerLog:
    """A mesh ledger directory as a CLL entry log, its checkpoint state and a
    record store: the two inputs of
    ``agent_action_capsule.bundle.proof_source_from_cll(log, log)``.

    Every line of ``capsules.jsonl`` is a leaf, in order, padding lines
    (``record_type: "padding"``) included; its value is the line's
    ``capsule_id``. Records are the lines that are not padding. ``get``
    returns a record's capsule bytes without the inline ``signature`` and
    ``key_id`` members, and the hex-decoded ``signature`` (a COSE_Sign1
    Producer Envelope) as ``producer_envelope``, empty for an unsigned line;
    a padding line or an unknown ID is ``not_found``.

    ``load_cll`` reads ``checkpoints.jsonl``: the newest ``mmr_checkpoint``
    line (largest ``mmr_size``) whose own signature verifies. Its COSE
    statement is the line's ``checkpoint_cose`` (hex) when present, which must
    verify and state the same size and root. Otherwise the statement is
    signed here with ``signing_key`` (the node's Ed25519 key, PKCS#8 PEM),
    which must exist (it is never created) and be the key that signed the
    checkpoint. The MMR nodes are folded from ``capsules.jsonl`` and must
    rebuild the checkpoint's root. A directory with no checkpoint yields a
    state with ``checkpoint=None``; every other refusal is
    :class:`MeshLedgerError`.

    The files are read once, when the log is opened, so entries, checkpoint
    and records describe the same moment.
    """

    def __init__(self, ledger_dir: str | os.PathLike, signing_key: str | os.PathLike | None = None) -> None:
        self._dir = Path(ledger_dir)
        self._signing_key = Path(signing_key) if signing_key is not None else None
        capsules = self._dir / "capsules.jsonl"
        if not capsules.is_file():
            raise MeshLedgerError(f"{self._dir}: no capsules.jsonl, so not a mesh ledger directory")
        self._lines: list[dict] = []
        self._entries: list[Entry] = []
        self._records: dict[str, int] = {}
        previous = _EPOCH
        for number, obj in _json_lines(capsules):
            capsule_id = obj.get("capsule_id")
            if not valid_id(capsule_id):
                raise LogCorruptError(f"{capsules}:{number}: capsule_id is not a 32-byte hex digest")
            seq = len(self._entries) + 1
            previous = _line_time(seq, obj) or previous
            self._entries.append(Entry(seq, bytes.fromhex(capsule_id), previous))
            self._lines.append(obj)
            if obj.get("record_type") != _PADDING:
                self._records.setdefault(capsule_id, len(self._lines) - 1)
        self._checkpoints = self._read_checkpoints()

    def _read_checkpoints(self) -> list[tuple[Any, str | None]]:
        from cll.checkpoint import CheckpointRecord, verify_checkpoint_signature_offline

        path = self._dir / "checkpoints.jsonl"
        if not path.is_file():
            return []
        found, good = 0, []
        for number, raw in _json_lines(path):
            if raw.get("kind") != _CHECKPOINT_KIND:
                continue
            found += 1
            try:
                cp = CheckpointRecord.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise LogCorruptError(f"{path}:{number}: not a checkpoint record ({exc})") from exc
            if verify_checkpoint_signature_offline(cp):
                cose = raw.get("checkpoint_cose")
                good.append((cp, cose if isinstance(cose, str) and cose else None))
        if found and not good:
            raise MeshLedgerError(f"{path}: no checkpoint whose signature verifies")
        return good

    # -- CLL entry log ---------------------------------------------------------

    def scan_entries(self, after_seq: int, limit: int) -> list[Entry]:
        check_scan(after_seq, limit)
        return self._entries[after_seq : after_seq + limit]

    # -- CLL checkpoint state, read-only ------------------------------------------

    def load_cll(self) -> CllState:
        if not self._checkpoints:
            return CllState()
        from cll.checkpoint import leaf_count

        cp, cose_hex = max(self._checkpoints, key=lambda pair: pair[0].mmr_size)
        values = [entry.value for entry in self._entries]
        if leaf_count(cp.mmr_size) > len(values):
            raise MeshLedgerError(
                f"checkpoint mmr_size={cp.mmr_size} covers {leaf_count(cp.mmr_size)} leaves; "
                f"capsules.jsonl has {len(values)}"
            )
        cose = self._stated_cose(cp, cose_hex) if cose_hex is not None else self._signed_cose(cp, values)
        return _cll_state_at(values, cose)

    def _stated_cose(self, cp: Any, cose_hex: str) -> bytes:
        from cll.checkpoint import verify_checkpoint_cose_offline

        try:
            cose = bytes.fromhex(cose_hex)
        except ValueError as exc:
            raise MeshLedgerError(f"checkpoint mmr_size={cp.mmr_size}: checkpoint_cose is not hex") from exc
        checked = verify_checkpoint_cose_offline(cose)
        if not checked.ok or checked.decoded is None:
            raise MeshLedgerError(f"checkpoint mmr_size={cp.mmr_size}: its checkpoint_cose does not verify")
        signed = checked.decoded
        # The statement must be this checkpoint, signed by its key, with its
        # time and history: every field the signature-verified line states.
        fields = ("mmr_size", "root", "log_id", "key_id", "timestamp", "prev_size", "prev_root")
        differs = [name for name in fields if getattr(signed, name) != getattr(cp, name)]
        if differs:
            raise MeshLedgerError(
                f"checkpoint mmr_size={cp.mmr_size}: its checkpoint_cose states another checkpoint ({', '.join(differs)})"
            )
        return cose

    def _signed_cose(self, cp: Any, values: list) -> bytes:
        """The checkpoint's COSE statement, signed with the node's key. A
        checkpoint that extends an earlier one carries the consistency proof
        the COSE form requires to claim it."""
        from cll.checkpoint import (
            MemoryNodeStore,
            add_leaf,
            checkpoint_to_cose,
            consistency_proof,
            leaf_count,
            leaf_hash,
            peaks,
        )

        from .signing import LocalKeypairSigner

        if self._signing_key is None:
            raise MeshLedgerError(
                f"checkpoint mmr_size={cp.mmr_size} carries no checkpoint_cose: pass the node's signing key "
                "(the key that signed the checkpoint) to give it its COSE form"
            )
        if not self._signing_key.is_file():
            raise MeshLedgerError(f"{self._signing_key}: no signing key there (it is never created)")
        signer = LocalKeypairSigner(self._signing_key)
        if signer.key_id != cp.key_id:
            raise MeshLedgerError("the signing key is not the key that signed the checkpoint")
        nodes = MemoryNodeStore()
        for value in values[: leaf_count(cp.mmr_size)]:
            add_leaf(nodes, leaf_hash(value))
        if nodes.size() != cp.mmr_size:
            raise MeshLedgerError(f"checkpoint mmr_size={cp.mmr_size} is not the size of its leaves' MMR")

        def peak_hashes(size: int) -> list:
            return [nodes.node(pos) for pos in peaks(size)]

        prev = cp.prev_size
        return checkpoint_to_cose(
            cp,
            signer,
            peak_hashes(cp.mmr_size),
            prev_peak_hashes=peak_hashes(prev) if prev else None,
            consistency_proof=consistency_proof(nodes, prev, cp.mmr_size) if prev else None,
        )

    def commit_cll(self, expected_size: int, expected_checkpoint: bytes | None, next: CllState) -> None:
        from cll.log import UnsupportedError as LogUnsupportedError

        raise LogUnsupportedError("a mesh ledger directory's checkpoints are written by its node")

    # -- record store ----------------------------------------------------------

    def get(self, capsule_id: str) -> Record:
        if not valid_id(capsule_id):
            raise InvalidError("capsule id")
        index = self._records.get(capsule_id)
        if index is None:
            raise NotFoundError("capsule not found")
        return checked_record(capsule_id, self._lines[index])

    def close(self) -> None:
        """Nothing to release: the files were read when the log was opened."""
