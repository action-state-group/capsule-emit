# SPDX-License-Identifier: Apache-2.0
"""``MeshLedgerLog`` (a mesh ledger directory as a proof source for
agent-action-capsule's Evidence Bundle producer) and ``capsule-emit bundle``
over it and over ``ledger.jsonl``.

The mesh directories are laid out as the Mesh-LLM capsule-emit-mesh plugin
writes them: sealed capsules (with their inline ``signature``/``key_id``) and
padding leaves in ``capsules.jsonl``, signed ``mmr_checkpoint`` lines in
``checkpoints.jsonl``, with or without the ``checkpoint_cose`` statement.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import secrets
from types import SimpleNamespace

import pytest
from agent_action_capsule.bundle import (
    BundleBuildError,
    build_evidence_bundle,
    proof_source_from_cll,
    verify_bundle,
)
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import cli, seal
from capsule_emit.evidence_bundle import MeshLedgerError, MeshLedgerLog
from capsule_emit.ledger import read_ledger
from capsule_emit.signing import LocalKeypairSigner

_NO_WITNESS = "http://127.0.0.1:1"


class _MemoryLog:
    def __init__(self) -> None:
        self.n = 0

    def append(self, capsule: dict, *, consequential: bool = True):  # noqa: ARG002
        self.n += 1
        return SimpleNamespace(seq=self.n, capsule_id=capsule["capsule_id"])

    def scan(self, *a, **k):  # noqa: ARG002
        return []


class _CheckpointSigner:
    """The checkpoint layer's signer (a hex digest in, a hex signature out),
    over the node's own key, as a node signs its checkpoints."""

    def __init__(self, inner: LocalKeypairSigner) -> None:
        self._inner = inner
        self.key_id = inner.key_id

    def sign(self, digest_hex: str) -> str:
        signature_hex, _ = self._inner.sign(digest_hex.encode("ascii"))
        return signature_hex


def _padding() -> dict:
    return {"capsule_id": secrets.token_hex(32), "record_type": "padding", "store_nonce": secrets.token_hex(16)}


def _mesh_ledger(tmp_path, *, padding=True, cose=True, extra_after_checkpoint=False, mesh=None):
    """A mesh ledger directory: three sealed capsules (a padding leaf after
    the first unless ``padding=False``), a first checkpoint over the first two
    leaves and a second over all of them, the second extending the first.
    ``cose`` writes each checkpoint's COSE statement beside it. Returns
    (dir, key_path, capsules)."""
    from cll.checkpoint import MmrLedger, checkpoint_to_cose, emit_checkpoint

    flat = tmp_path / "flat.jsonl"
    for n, action in enumerate(("first", "second", "third")):
        extra = (mesh or {}).get(n, {})
        seal(None, action=action, operator="acme", anchor=False, ledger=flat, witness_url=_NO_WITNESS, **extra)
    capsules = read_ledger(flat)
    lines = [capsules[0], _padding(), capsules[1], capsules[2]] if padding else list(capsules)

    key = tmp_path / "node-key.pem"
    signer = LocalKeypairSigner(key)
    mmr = MmrLedger(_MemoryLog())
    checkpoints = []
    for n, line in enumerate(lines, 1):
        mmr.append({"capsule_id": line["capsule_id"]})
        if n in (2, len(lines)):
            prev = checkpoints[-1][0] if checkpoints else None
            cp = emit_checkpoint(mmr, _CheckpointSigner(signer), log_id="test/mesh-node", prev=prev)
            statement = checkpoint_to_cose(
                cp,
                signer,
                mmr.peak_hashes_at(cp.mmr_size),
                prev_peak_hashes=mmr.peak_hashes_at(prev.mmr_size) if prev else None,
                consistency_proof=mmr.consistency_proof(prev.mmr_size, cp.mmr_size) if prev else None,
            )
            checkpoints.append((cp, statement))
    if extra_after_checkpoint:
        seal(None, action="late", operator="acme", anchor=False, ledger=flat, witness_url=_NO_WITNESS)
        late = read_ledger(flat)[-1]
        lines.append(late)
        capsules.append(late)

    d = tmp_path / "ledger"
    d.mkdir()
    (d / "capsules.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    rows = []
    for cp, statement in checkpoints:
        row = dataclasses.asdict(cp)
        if cose:
            row["checkpoint_cose"] = statement.hex()
        rows.append(json.dumps(row, sort_keys=True) + "\n")
    (d / "checkpoints.jsonl").write_text("".join(rows))
    return d, key, capsules


def _build(log, **kwargs):
    return build_evidence_bundle(proof_source_from_cll(log, log), **kwargs)


# -- MeshLedgerLog ------------------------------------------------------------


def test_every_line_is_a_leaf_and_padding_is_never_a_record(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path)
    log = MeshLedgerLog(d)
    entries = log.scan_entries(0, 10)
    assert [e.seq for e in entries] == [1, 2, 3, 4]
    padding = entries[1].value.hex()
    with pytest.raises(Exception) as caught:
        log.get(padding)
    assert caught.value.code == "not_found"
    with pytest.raises(Exception) as caught:
        log.get(secrets.token_hex(32))
    assert caught.value.code == "not_found"

    record = log.get(capsules[1]["capsule_id"])
    stored = json.loads(record.capsule)
    assert "signature" not in stored and "key_id" not in stored
    assert record.producer_envelope == bytes.fromhex(capsules[1]["signature"])
    assert len(read_ledger(d / "capsules.jsonl")) == len(capsules), "read_ledger skips padding too"


def test_the_checkpoint_line_cose_is_used_as_is(tmp_path):
    d, _, _ = _mesh_ledger(tmp_path)
    rows = [json.loads(line) for line in (d / "checkpoints.jsonl").read_text().splitlines()]
    state = MeshLedgerLog(d).load_cll()
    assert state.checkpoint.bytes == bytes.fromhex(rows[-1]["checkpoint_cose"])
    assert state.checkpoint.indexed_seq == state.indexed_seq == 4


def test_without_cose_the_node_key_signs_the_newest_checkpoint(tmp_path):
    from cll.checkpoint import verify_checkpoint_cose_offline

    d, key, capsules = _mesh_ledger(tmp_path, cose=False)
    state = MeshLedgerLog(d, signing_key=key).load_cll()
    signed = verify_checkpoint_cose_offline(state.checkpoint.bytes)
    assert signed.ok and signed.decoded.mmr_size == state.checkpoint.size
    assert signed.decoded.prev_size > 0, "the newest checkpoint extends the first, and says so with a proof"

    bundle = _build(MeshLedgerLog(d, signing_key=key), root=capsules[2]["capsule_id"], interval_from=3)
    assert verify_bundle(bundle, strict=True).verdict == "VALID"


def test_without_cose_or_key_the_log_is_refused_by_name(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path, cose=False)
    with pytest.raises(MeshLedgerError, match="carries no checkpoint_cose: pass the node's signing key"):
        MeshLedgerLog(d).load_cll()


def test_the_key_must_be_the_checkpoint_key_and_is_never_created(tmp_path):
    d, _, _ = _mesh_ledger(tmp_path, cose=False)
    other = tmp_path / "other.pem"
    LocalKeypairSigner(other)
    with pytest.raises(MeshLedgerError, match="not the key that signed"):
        MeshLedgerLog(d, signing_key=other).load_cll()
    missing = tmp_path / "missing.pem"
    with pytest.raises(MeshLedgerError, match="never created"):
        MeshLedgerLog(d, signing_key=missing).load_cll()
    assert not missing.exists()


def test_no_checkpoints_means_no_checkpoint_state(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path)
    (d / "checkpoints.jsonl").unlink()
    log = MeshLedgerLog(d)
    assert log.load_cll().checkpoint is None
    with pytest.raises(BundleBuildError, match="no checkpoint"):
        _build(log, root=capsules[0]["capsule_id"], interval_from=None)


def test_a_checkpoint_whose_signature_fails_is_never_used(tmp_path):
    d, _, _ = _mesh_ledger(tmp_path)
    rows = [json.loads(line) for line in (d / "checkpoints.jsonl").read_text().splitlines()]
    for row in rows:
        row["signature"] = ("0" if row["signature"][0] != "0" else "1") + row["signature"][1:]
    (d / "checkpoints.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(MeshLedgerError, match="no checkpoint whose signature verifies"):
        MeshLedgerLog(d)


def test_a_cose_statement_that_does_not_verify_is_refused(tmp_path):
    d, _, _ = _mesh_ledger(tmp_path)
    rows = [json.loads(line) for line in (d / "checkpoints.jsonl").read_text().splitlines()]
    cose = rows[-1]["checkpoint_cose"]
    rows[-1]["checkpoint_cose"] = cose[:-2] + ("00" if cose[-2:] != "00" else "01")
    (d / "checkpoints.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(MeshLedgerError, match="checkpoint_cose does not verify"):
        MeshLedgerLog(d).load_cll()


def test_a_ledger_that_does_not_rebuild_the_root_is_corrupt(tmp_path):
    from cll.log import CorruptError

    d, _, _ = _mesh_ledger(tmp_path)
    lines = (d / "capsules.jsonl").read_text().splitlines()
    (d / "capsules.jsonl").write_text("\n".join([lines[0], lines[2], lines[1], lines[3]]) + "\n")
    with pytest.raises(CorruptError, match="do not rebuild the root"):
        MeshLedgerLog(d).load_cll()


def test_one_record_is_proven_alone_without_a_range(tmp_path):
    from cll.checkpoint import InclusionProof, verify_checkpoint_cose_offline, verify_inclusion

    d, _, capsules = _mesh_ledger(tmp_path)
    target = capsules[1]
    bundle = _build(MeshLedgerLog(d), root=target["capsule_id"], interval_from=None, closure_depth=0)

    assert [r["capsule_id"] for r in bundle["records"]] == [target["capsule_id"]]
    assert bundle["completeness"]["selection"] == "producer-selected"
    assert "range_proof" not in bundle["completeness_certificate"]
    assert verify_bundle(bundle, strict=True).verdict == "INCOMPLETE"

    cp = bundle["checkpoint"]
    cose = base64.urlsafe_b64decode(cp["cose"] + "=" * (-len(cp["cose"]) % 4))
    decoded = verify_checkpoint_cose_offline(cose)
    assert decoded.ok and decoded.decoded.root == cp["root"] and decoded.decoded.mmr_size == cp["mmr_size"]

    member = bundle["completeness_certificate"]["memberships"][target["capsule_id"]]
    assert member["log_coordinates"]["seq"] == 3, "the padding leaf counts as a leaf"
    proof = InclusionProof(**{k: tuple(v) if isinstance(v, list) else v for k, v in member["inclusion_proof"].items()})
    assert verify_inclusion(bytes.fromhex(cp["root"]), cp["mmr_size"], 2, bytes.fromhex(target["capsule_id"]), proof)


def test_a_range_over_padding_is_refused_and_one_after_it_is_valid(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path)
    log = MeshLedgerLog(d)
    with pytest.raises(BundleBuildError, match="seq 2 in the interval is not a record"):
        _build(log, root=capsules[2]["capsule_id"], interval_from=1)
    bundle = _build(log, root=capsules[2]["capsule_id"], interval_from=3)
    assert verify_bundle(bundle, strict=True).verdict == "VALID"


def test_a_record_the_checkpoint_does_not_cover_is_refused(tmp_path):
    d, _, capsules = _mesh_ledger(tmp_path, extra_after_checkpoint=True)
    with pytest.raises(BundleBuildError, match="not a leaf at or below the checkpoint"):
        _build(MeshLedgerLog(d), root=capsules[-1]["capsule_id"], interval_from=None)


def test_a_changed_proof_or_root_in_a_bundle_is_invalid(tmp_path):
    from capsule_emit.evidence_file import check_evidence_file

    d, _, capsules = _mesh_ledger(tmp_path)
    target = capsules[1]["capsule_id"]
    bundle = _build(MeshLedgerLog(d), root=target, interval_from=None, closure_depth=0)
    assert check_evidence_file(bundle).verdict == "INCOMPLETE"

    changed = json.loads(json.dumps(bundle))
    witness = changed["completeness_certificate"]["memberships"][target]["inclusion_proof"]["witness"]
    witness[0] = ("0" if witness[0][0] != "0" else "1") + witness[0][1:]
    check = check_evidence_file(changed)
    assert check.verdict == "INVALID"
    assert next(c for c in check.checks if c.name == "per_record_membership").status == "fail"

    changed = json.loads(json.dumps(bundle))
    root = changed["completeness_certificate"]["range_root"]
    changed["completeness_certificate"]["range_root"] = ("0" if root[0] != "0" else "1") + root[1:]
    assert check_evidence_file(changed).verdict == "INVALID"


# -- capsule-emit bundle --------------------------------------------------------


def _mesh_fields(exchange: str, server: str, requester: str) -> dict:
    """``seal()`` keywords that put Mesh-LLM's serving provenance in the
    capsule, where the plugin puts it."""
    provenance = {"exchange_id": exchange, "served_by_node_id": server, "requesting_party": requester}
    return {"extra_compute": {"x-mesh-poc-v1": {"serving_provenance": provenance}}}


def _cli(capsys, *argv):
    code = cli.main(["bundle", *map(str, argv)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_a_scope_is_required(tmp_path, capsys):
    d, _, capsules = _mesh_ledger(tmp_path)
    out = tmp_path / "b.json"
    code, _, err = _cli(capsys, d, "-o", out)
    assert code == 1 and "exactly one scope" in err
    code, _, err = _cli(capsys, d, "--record", capsules[0]["capsule_id"], "--all", "-o", out)
    assert code == 1 and "exactly one scope" in err
    code, _, err = _cli(capsys, d, "--all", "--interval-from", "3", "-o", out)
    assert code == 1 and "apply to a scoped bundle" in err
    assert not out.exists()


def test_cli_mesh_all_is_valid_and_a_one_byte_tamper_is_invalid(tmp_path, capsys):
    from agent_action_capsule.canonical import jcs

    d, _, capsules = _mesh_ledger(tmp_path, padding=False)
    out = tmp_path / "all.json"
    code, stdout, err = _cli(capsys, d, "--all", "-o", out)
    assert code == 0, err
    assert "VALID" in stdout
    raw = out.read_bytes()
    bundle = json.loads(raw)
    assert jcs(bundle) == raw
    assert [r["capsule_id"] for r in bundle["records"]] == [c["capsule_id"] for c in capsules]
    assert bundle["completeness_certificate"]["first_seq"] == 1
    assert verify_bundle(bundle, strict=True).verdict == "VALID"

    target = capsules[1]["capsule_id"]
    index = raw.index(target.encode()) + 10
    tampered = raw[:index] + (b"0" if raw[index : index + 1] != b"0" else b"1") + raw[index + 1 :]
    assert verify_bundle(json.loads(tampered), strict=True).verdict == "INVALID"


def test_cli_mesh_all_over_padding_is_refused_by_name(tmp_path, capsys):
    d, _, _ = _mesh_ledger(tmp_path)
    code, _, err = _cli(capsys, d, "--all", "-o", tmp_path / "b.json")
    assert code == 1 and "seq 2 in the interval is not a record" in err


def test_cli_scopes_read_the_mesh_fields(tmp_path, capsys):
    mesh = {
        0: _mesh_fields("ex-1", "node-a", "node-b"),
        1: _mesh_fields("ex-2", "node-a", "node-c"),
        2: _mesh_fields("ex-1", "node-a", "node-b"),
    }
    d, _, capsules = _mesh_ledger(tmp_path, mesh=mesh)
    out = tmp_path / "b.json"

    code, _, err = _cli(capsys, d, "--exchange", "ex-1", "-o", out)
    assert code == 0, err
    bundle = json.loads(out.read_bytes())
    assert [r["capsule_id"] for r in bundle["records"]] == [capsules[0]["capsule_id"], capsules[2]["capsule_id"]]
    assert bundle["root"] == capsules[2]["capsule_id"]
    assert bundle["completeness"]["closure_depth"] == 0
    assert bundle["completeness"]["selection"] == "producer-selected"
    assert verify_bundle(bundle, strict=True).verdict == "INCOMPLETE"

    code, _, err = _cli(capsys, d, "--peer", "node-c", "-o", out)
    assert code == 0, err
    assert [r["capsule_id"] for r in json.loads(out.read_bytes())["records"]] == [capsules[1]["capsule_id"]]

    code, _, err = _cli(capsys, d, "--exchange", "ex-1", "--interval-from", "3", "-o", out)
    assert code == 1 and "below interval_from 3" in err

    code, _, err = _cli(capsys, d, "--peer", "node-c", "--interval-from", "3", "-o", out)
    assert code == 0, err
    bundle = json.loads(out.read_bytes())
    assert [r["capsule_id"] for r in bundle["records"]] == [capsules[1]["capsule_id"], capsules[2]["capsule_id"]]
    assert verify_bundle(bundle, strict=True).verdict == "VALID"

    code, _, err = _cli(capsys, d, "--exchange", "ex-9", "-o", out)
    assert code == 1 and "no record in scope" in err


def test_cli_time_window_and_record_scopes(tmp_path, capsys):
    d, _, capsules = _mesh_ledger(tmp_path)
    out = tmp_path / "b.json"
    code, _, err = _cli(capsys, d, "--since", capsules[1]["timestamp"], "-o", out)
    assert code == 0, err
    ids = [r["capsule_id"] for r in json.loads(out.read_bytes())["records"]]
    assert ids == [c["capsule_id"] for c in capsules if c["timestamp"] >= capsules[1]["timestamp"]]

    code, _, err = _cli(capsys, d, "--until", "2000-01-01T00:00:00Z", "-o", out)
    assert code == 1 and "no record in scope" in err

    code, _, err = _cli(capsys, d, "--record", capsules[0]["capsule_id"], "-o", out)
    assert code == 0, err
    assert [r["capsule_id"] for r in json.loads(out.read_bytes())["records"]] == [capsules[0]["capsule_id"]]


def test_cli_refuses_a_ledger_jsonl_without_a_checkpoint(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    capsule = seal(None, action="only", operator="acme", anchor=False, ledger=flat, witness_url=_NO_WITNESS).capsule
    code, _, err = _cli(capsys, flat, "--record", capsule["capsule_id"], "-o", tmp_path / "b.json")
    assert code == 1 and "no checkpoint" in err


def test_cli_mesh_without_cose_needs_the_signing_key(tmp_path, capsys):
    d, key, capsules = _mesh_ledger(tmp_path, cose=False)
    out = tmp_path / "b.json"
    code, _, err = _cli(capsys, d, "--record", capsules[0]["capsule_id"], "-o", out)
    assert code == 1 and "pass the node's signing key" in err
    code, _, err = _cli(capsys, d, "--record", capsules[0]["capsule_id"], "--signing-key", key, "-o", out)
    assert code == 0, err


def test_cli_refuses_a_segmented_store_by_name(tmp_path, capsys):
    store = tmp_path / "store"
    store.mkdir()
    (store / "manifest.json").write_text("{}")
    code, _, err = _cli(capsys, store, "--all", "-o", tmp_path / "b.json")
    assert code == 1
    assert "is a Python mesh LedgerStore directory (a frozen harness); capsule-emit bundle reads the flat" in err


def test_cli_refuses_a_directory_that_is_not_a_mesh_ledger(tmp_path, capsys):
    code, _, err = _cli(capsys, tmp_path, "--all", "-o", tmp_path / "b.json")
    assert code == 1 and "no checkpoints.jsonl" in err


@pytest.fixture
def witnessed_ledger(tmp_path, monkeypatch):
    """A ``ledger.jsonl``: c0, c1 (confirms c0), stamp 1, c2, c3 (confirms
    c2), stamp 2 over the first five leaves."""
    url, _, stop = _start_stub_ts()
    monkeypatch.setenv("CAPSULE_WITNESS_URL", url)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    path = tmp_path / "ledger.jsonl"

    def _seal(action, **kwargs):
        return seal(None, action=action, operator="acme", anchor=False, ledger=path, witness_url=url, **kwargs).capsule

    c0 = _seal("c0")
    c1 = _seal("c1", confirms=c0["capsule_id"])
    assert _wait_for(lambda: _stamp_count(path) >= 1)
    c2 = _seal("c2")
    c3 = _seal("c3", confirms=c2["capsule_id"])
    assert _wait_for(lambda: _stamp_count(path) >= 2)
    yield path, [c0, c1, c2, c3]
    stop()


def test_cli_flat_ledger_scopes(witnessed_ledger, tmp_path, capsys):
    path, caps = witnessed_ledger
    out = tmp_path / "b.json"

    code, _, err = _cli(capsys, path, "--all", "-o", out)
    assert code == 1 and "seq 3 in the interval is not a record" in err

    code, stdout, err = _cli(capsys, path, "--since", caps[2]["timestamp"], "--interval-from", "4", "-o", out)
    assert code == 0, err
    assert "VALID" in stdout and "INCOMPLETE" not in stdout
    bundle = json.loads(out.read_bytes())
    assert [r["capsule_id"] for r in bundle["records"]] == [caps[2]["capsule_id"], caps[3]["capsule_id"]]

    code, _, err = _cli(capsys, path, "--record", caps[1]["capsule_id"], "--closure-depth", "1", "-o", out)
    assert code == 0, err
    bundle = json.loads(out.read_bytes())
    assert [r["capsule_id"] for r in bundle["records"]] == [caps[0]["capsule_id"], caps[1]["capsule_id"]]
    assert verify_bundle(bundle, strict=True).verdict == "INCOMPLETE"

    code, _, err = _cli(capsys, path, "--record", caps[1]["capsule_id"], "--signing-key", tmp_path / "k.pem", "-o", out)
    assert code == 1 and "--signing-key applies to a mesh ledger directory" in err


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda line: line.pop("signature"), "must both be present"),
        (lambda line: line.pop("key_id"), "must both be present"),
        (lambda line: line.__setitem__("key_id", "ab" * 32), "is not the envelope's signer"),
        (lambda line: line.__setitem__("signature", ""), "must both be present|empty"),
    ],
)
def test_a_mesh_record_with_a_broken_signature_claim_is_refused(tmp_path, edit, message):
    from capsule_emit.artifact import InvalidError
    from capsule_emit.evidence_bundle import MeshLedgerLog

    d, _, capsules = _mesh_ledger(tmp_path, padding=False)
    lines = [json.loads(x) for x in (d / "capsules.jsonl").read_text().splitlines()]
    assert "signature" in lines[0] and "key_id" in lines[0]
    edit(lines[0])
    (d / "capsules.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    with pytest.raises(InvalidError, match=message):
        MeshLedgerLog(d).get(capsules[0]["capsule_id"])


@pytest.mark.parametrize("change", [{"timestamp": "2001-01-01T00:00:00Z"}, {"key_id": "other"}])
def test_a_checkpoint_cose_that_states_other_fields_is_refused(tmp_path, change):
    """The newest line's statement is re-signed over the same size and root
    but with another timestamp, or by another key: only that field differs."""
    from cll.checkpoint import MmrLedger, checkpoint_to_cose

    from capsule_emit.evidence_bundle import MeshLedgerError, MeshLedgerLog

    d, key, _ = _mesh_ledger(tmp_path, padding=False)
    rows = [json.loads(x) for x in (d / "checkpoints.jsonl").read_text().splitlines()]
    newest = max(rows, key=lambda r: r["mmr_size"])
    prev = min(rows, key=lambda r: r["mmr_size"])
    mmr = MmrLedger(_MemoryLog())
    for line in (d / "capsules.jsonl").read_text().splitlines():
        mmr.append({"capsule_id": json.loads(line)["capsule_id"]})
    signer = LocalKeypairSigner(key)
    if "key_id" in change:
        signer = LocalKeypairSigner(tmp_path / "other-key.pem")
        change = {"key_id": signer.key_id}
    from cll.checkpoint import CheckpointRecord

    cp = CheckpointRecord.from_dict({k: v for k, v in newest.items() if k != "checkpoint_cose"})
    other = dataclasses.replace(cp, **change)
    newest["checkpoint_cose"] = checkpoint_to_cose(
        other,
        signer,
        mmr.peak_hashes_at(cp.mmr_size),
        prev_peak_hashes=mmr.peak_hashes_at(prev["mmr_size"]),
        consistency_proof=mmr.consistency_proof(prev["mmr_size"], cp.mmr_size),
    ).hex()
    (d / "checkpoints.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    field = next(iter(change))
    with pytest.raises(MeshLedgerError, match=f"states another checkpoint \\({field}\\)"):
        MeshLedgerLog(d).load_cll()
