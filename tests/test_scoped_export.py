# SPDX-License-Identifier: Apache-2.0
"""``capsule-emit export``: a scoped evidence file, each record proven in the
node's signed checkpoint; and the two guards around it (padding is never a
record; a whole-ledger evidence file needs ``--all``)."""

from __future__ import annotations

import base64
import dataclasses
import json
import secrets

import pytest

from capsule_emit import cli, seal
from capsule_emit.ledger import read_ledger
from capsule_emit.scoped_export import ExportError, Scope, _in_scope, export
from capsule_emit.signing import LocalKeypairSigner


class _MemoryLog:
    def __init__(self) -> None:
        self.n = 0

    def append(self, capsule: dict, *, consequential: bool = True):  # noqa: ARG002
        from types import SimpleNamespace

        self.n += 1
        return SimpleNamespace(seq=self.n, capsule_id=capsule["capsule_id"])

    def scan(self, *a, **k):  # noqa: ARG002
        return []


class _CheckpointSigner:
    """The checkpoint layer's signer (a hex digest in, a hex signature out),
    over the node's own key -- as a node signs its checkpoints."""

    def __init__(self, inner: LocalKeypairSigner) -> None:
        self._inner = inner
        self.key_id = inner.key_id

    def sign(self, digest_hex: str) -> str:
        signature_hex, _ = self._inner.sign(digest_hex.encode("ascii"))
        return signature_hex


def _padding() -> dict:
    return {"capsule_id": secrets.token_hex(32), "record_type": "padding", "store_nonce": secrets.token_hex(16)}


def _mesh_ledger(tmp_path, *, extra_after_checkpoint: bool = False):
    """A ledger directory laid out as the mesh plugin writes it: sealed
    capsules with a padding leaf among them, and a signed checkpoint in
    checkpoints.jsonl. Returns (dir, key_path, capsules)."""
    from cll.checkpoint import MmrLedger, emit_checkpoint

    flat = tmp_path / "flat.jsonl"
    for action in ("first", "second", "third"):
        seal(None, action=action, operator="acme", anchor=False, ledger=flat, witness_url="http://127.0.0.1:1")
    capsules = read_ledger(flat)
    lines = [capsules[0], _padding(), capsules[1], capsules[2]]

    key = tmp_path / "node-key.pem"
    signer = LocalKeypairSigner(key)
    mmr = MmrLedger(_MemoryLog())
    for line in lines:
        mmr.append({"capsule_id": line["capsule_id"]})
    cp = emit_checkpoint(mmr, _CheckpointSigner(signer), log_id="test/mesh-node")
    if extra_after_checkpoint:
        seal(None, action="late", operator="acme", anchor=False, ledger=flat, witness_url="http://127.0.0.1:1")
        late = read_ledger(flat)[-1]
        lines.append(late)
        capsules.append(late)

    d = tmp_path / "ledger"
    d.mkdir()
    (d / "capsules.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (d / "checkpoints.jsonl").write_text(json.dumps(dataclasses.asdict(cp)) + "\n")
    return d, key, capsules


def test_a_scope_is_required(tmp_path):
    d, key, _ = _mesh_ledger(tmp_path)
    with pytest.raises(ExportError, match="exactly one scope"):
        export(d, Scope(), signing_key=key)
    with pytest.raises(ExportError, match="exactly one scope"):
        export(d, Scope(record="x", all=True), signing_key=key)


def test_one_record_is_exported_alone_and_proven(tmp_path):
    from cll.checkpoint import InclusionProof, verify_checkpoint_cose_offline, verify_inclusion

    d, key, capsules = _mesh_ledger(tmp_path)
    target = capsules[1]
    bundle = export(d, Scope(record=target["capsule_id"]), signing_key=key)

    assert [r["capsule_id"] for r in bundle["records"]] == [target["capsule_id"]]
    assert bundle["completeness"]["selection"] == "producer-selected"
    assert "range_proof" not in bundle["completeness_certificate"]

    cp = bundle["checkpoint"]
    cose = base64.urlsafe_b64decode(cp["cose"] + "=" * (-len(cp["cose"]) % 4))
    decoded = verify_checkpoint_cose_offline(cose)
    assert decoded.ok and decoded.decoded.root == cp["root"] and decoded.decoded.mmr_size == cp["mmr_size"]

    member = bundle["completeness_certificate"]["memberships"][target["capsule_id"]]
    assert member["log_coordinates"]["seq"] == 3, "the padding leaf counts as a leaf"
    proof = InclusionProof(**{k: tuple(v) if isinstance(v, list) else v for k, v in member["inclusion_proof"].items()})
    assert verify_inclusion(bytes.fromhex(cp["root"]), cp["mmr_size"], 2, bytes.fromhex(target["capsule_id"]), proof)


def test_padding_is_never_a_record(tmp_path):
    d, key, capsules = _mesh_ledger(tmp_path)
    bundle = export(d, Scope(all=True), signing_key=key)
    assert len(bundle["records"]) == len(capsules)
    assert all(r.get("record_type") != "padding" for r in bundle["records"])
    assert len(read_ledger(d / "capsules.jsonl")) == len(capsules), "read_ledger skips padding too"


def test_a_record_the_checkpoint_does_not_cover_is_refused(tmp_path):
    d, key, capsules = _mesh_ledger(tmp_path, extra_after_checkpoint=True)
    with pytest.raises(ExportError, match="not yet covered by a checkpoint"):
        export(d, Scope(record=capsules[-1]["capsule_id"]), signing_key=key)


def test_the_key_must_be_the_checkpoint_key_and_is_never_created(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path)
    other = tmp_path / "other.pem"
    LocalKeypairSigner(other)
    with pytest.raises(ExportError, match="not the key that signed"):
        export(d, Scope(record=capsules[0]["capsule_id"]), signing_key=other)
    missing = tmp_path / "missing.pem"
    with pytest.raises(ExportError, match="never creates one"):
        export(d, Scope(record=capsules[0]["capsule_id"]), signing_key=missing)
    assert not missing.exists()


def test_exchange_and_peer_scopes_read_the_mesh_fields():
    record = {
        "timestamp": "2026-10-04T03:50:00Z",
        "model_attestation": {
            "compute_attestation": {
                "x-mesh-poc-v1": {
                    "serving_provenance": {"exchange_id": "ex-1", "served_by_node_id": "node-a", "requesting_party": "node-b"}
                }
            }
        },
    }
    assert _in_scope(record, Scope(exchange="ex-1"))
    assert not _in_scope(record, Scope(exchange="ex-2"))
    assert _in_scope(record, Scope(peer="node-b"))
    assert not _in_scope(record, Scope(peer="node-c"))
    assert _in_scope(record, Scope(since="2026-10-04T00:00:00Z"))
    assert not _in_scope(record, Scope(until="2026-10-03T23:59:59Z"))


def test_the_scoped_file_reads_as_proven_where_the_verifier_knows_selected_files(tmp_path):
    bundle_mod = pytest.importorskip("agent_action_capsule.bundle")
    if not hasattr(bundle_mod, "PRODUCER_SELECTED"):
        pytest.skip("this agent-action-capsule verifier does not know producer-selected files yet")
    from capsule_emit.evidence_file import check_evidence_file

    d, key, capsules = _mesh_ledger(tmp_path)
    bundle = export(d, Scope(record=capsules[1]["capsule_id"]), signing_key=key)
    check = check_evidence_file(bundle)
    status = {c.name: (c.status, c.findings) for c in check.checks}
    assert status["checkpoint"][0] == "pass"
    assert status["per_record_membership"][0] == "pass"
    assert status["interval_coverage"] == ("withheld", ("interval_not_claimed",))
    assert check.ok


def test_permalink_bundle_out_of_a_whole_ledger_needs_all(tmp_path, capsys):
    flat = tmp_path / "flat.jsonl"
    for action in ("first", "second"):
        seal(None, action=action, operator="acme", anchor=False, ledger=flat, witness_url="http://127.0.0.1:1")
    out = tmp_path / "bundle.json"
    assert cli.main(["permalink", "--ledger", str(flat), "--bundle-out", str(out)]) == 1
    assert "whole history" in capsys.readouterr().err
    assert not out.exists()
    assert cli.main(["permalink", "--ledger", str(flat), "--bundle-out", str(out), "--all"]) == 0
    assert out.exists()
