# SPDX-License-Identifier: Apache-2.0
"""``capsule-emit verify --bundle`` and ``capsule-emit report``: an evidence
file (``evidence-bundle/v2``) checked offline, end to end, from capsules this
package seals and a checkpoint signed by the same key."""
from __future__ import annotations

import base64
import json
import re

import pytest

pytest.importorskip("cll.checkpoint.index")

from cll.checkpoint import (  # noqa: E402
    CheckpointRecord,
    MemoryNodeStore,
    add_leaf,
    checkpoint_to_cose,
    core,
    leaf_hash,
    peaks,
    root_from_peaks,
)

from capsule_emit import (  # noqa: E402
    LocalKeypairSigner,
    ReferenceEntry,  # noqa: E402
    seal,
)
from capsule_emit.cli import main  # noqa: E402
from capsule_emit.evidence_file import check_evidence_file  # noqa: E402
from capsule_emit.evidence_report import render_report_html  # noqa: E402


def _proof(p) -> dict:
    return {
        "v": p.v,
        "kind": p.kind,
        "size": p.size,
        "leaf_index": p.leaf_index,
        "witness": list(p.witness),
        "peaks_left": list(p.peaks_left),
        "peaks_right": list(p.peaks_right),
    }


def build_bundle(tmp_path, *, n=3, cose=True, unsigned_index=None, root_cites=None) -> dict:
    """A bundle over a whole n-record log, built the way a producer does:
    leaves are capsule ids, the checkpoint is signed by the records' key.
    ``root_cites``: a capsule digest the newest record references."""
    signer = LocalKeypairSigner(tmp_path / "k.pem")
    caps = [seal({"step": i}, anchor=False, witness=False, signer=signer).capsule for i in range(n - 1)]
    refs = (ReferenceEntry(type="agent-action-capsule", digest_alg="SHA-256", digest=root_cites),) if root_cites else None
    caps.append(seal({"step": n - 1}, references=refs, anchor=False, witness=False, signer=signer).capsule)
    if unsigned_index is not None:
        del caps[unsigned_index]["signature"], caps[unsigned_index]["key_id"]
    store = MemoryNodeStore()
    for cap in caps:
        add_leaf(store, leaf_hash(bytes.fromhex(cap["capsule_id"])))
    size = store.size()
    peak_hashes = [store.node(p) for p in peaks(size)]
    root = root_from_peaks(peak_hashes).hex()
    cp = CheckpointRecord(
        v=1, kind="mmr_checkpoint", log_id="test-log", mmr_size=size, root=root, prev_size=0,
        prev_root="", key_id=signer.key_id, timestamp="2026-10-01T00:00:00Z", signature="",
    )
    checkpoint = {k: v for k, v in cp.__dict__.items()}
    if cose:
        raw = checkpoint_to_cose(cp, signer, peak_hashes)
        checkpoint["cose"] = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    rp = core.range_proof(store, 0, n - 1, size)
    ids = [c["capsule_id"] for c in caps]
    return {
        "bundle_version": "2",
        "bundle_kind": "evidence-bundle/v2",
        "root": ids[-1],
        "records": caps,
        "completeness": {
            "closure_depth": 0, "records_mode": "complete", "payloads_mode": "selected", "suppressed_fields": [],
        },
        "completeness_certificate": {
            "log_id": "test-log", "range_root": root, "first_seq": 1, "last_seq": n, "body_digests": ids,
            "range_proof": {
                "from_seq": 1, "to_seq": n, "size": rp.size, "from_index": rp.from_index,
                "to_index": rp.to_index, "witness": list(rp.witness),
            },
            "memberships": {
                cid: {
                    "log_coordinates": {"log_id": "test-log", "seq": i + 1, "leaf_index": i},
                    "inclusion_proof": _proof(core.inclusion_proof(store, i, size)),
                }
                for i, cid in enumerate(ids)
            },
        },
        "checkpoint": checkpoint,
    }


def _status(check) -> dict:
    return {c.name: c.status for c in check.checks}


def test_a_whole_file_checks_and_the_checkpoint_is_authenticated(tmp_path):
    check = check_evidence_file(build_bundle(tmp_path))
    assert check.ok and check.proven
    assert set(_status(check).values()) == {"pass"}
    assert check.checkpoint_authenticated
    assert check.signer_matches_checkpoint is True
    assert [r.seq for r in check.records] == [1, 2, 3]


def test_without_a_cose_checkpoint_the_proofs_are_relative_to_the_stated_checkpoint(tmp_path):
    check = check_evidence_file(build_bundle(tmp_path, cose=False))
    assert check.ok
    assert not check.checkpoint_authenticated
    interval = next(c for c in check.checks if c.name == "interval_coverage")
    assert interval.status == "pass" and "checkpoint_unverified" in interval.findings


def test_an_edited_record_fails_identity_signature_and_membership(tmp_path):
    bundle = build_bundle(tmp_path)
    bundle["records"][1]["timestamp"] = "2020-01-01T00:00:00Z"
    check = check_evidence_file(bundle)
    assert not check.ok
    status = _status(check)
    assert status["records"] == status["signatures"] == status["per_record_membership"] == "fail"


def test_a_bad_inclusion_proof_fails_membership_only(tmp_path):
    bundle = build_bundle(tmp_path)
    member = next(iter(bundle["completeness_certificate"]["memberships"].values()))
    member["inclusion_proof"]["witness"][0] = "00" * 32
    status = _status(check_evidence_file(bundle))
    assert status["per_record_membership"] == "fail"
    assert status["records"] == status["signatures"] == "pass"


def test_a_record_signed_by_another_key_is_reported_not_gated(tmp_path):
    bundle = build_bundle(tmp_path)
    bundle["checkpoint"]["key_id"] = "ab" * 32
    check = check_evidence_file(bundle)
    assert check.signer_matches_checkpoint is False


def test_an_unsigned_record_is_not_shown_unless_signatures_are_required(tmp_path):
    bundle = build_bundle(tmp_path, unsigned_index=0)
    assert _status(check_evidence_file(bundle))["signatures"] == "withheld"
    assert check_evidence_file(bundle).ok
    assert not check_evidence_file(bundle, require_signature=True).ok


def test_a_declared_missing_citation_is_not_shown_and_an_undeclared_one_fails(tmp_path):
    absent = "cd" * 32
    bundle = build_bundle(tmp_path, root_cites=absent)
    bundle["completeness"].update(closure_depth=1)
    undeclared = check_evidence_file(bundle)
    assert _status(undeclared)["graph_closure"] == "fail"

    bundle["completeness"].update(records_mode="declared_incomplete", missing=[absent])
    declared = check_evidence_file(bundle)
    assert _status(declared)["graph_closure"] == "withheld"
    assert declared.ok and not declared.proven
    assert declared.missing == (absent,)


def test_a_file_that_is_not_an_evidence_bundle_is_refused(tmp_path):
    check = check_evidence_file({"schema": "some-pane-export/1", "rows": []})
    assert not check.kind_ok and not check.ok


def test_cli_verify_bundle_exit_codes_and_json(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(build_bundle(tmp_path)))
    assert main(["verify", "--bundle", str(good)]) == 0
    assert "VALID" in capsys.readouterr().out
    assert main(["verify", "--bundle", str(good), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["proven"] is True

    bad = json.loads(good.read_text())
    bad["records"][0]["timestamp"] = "2020-01-01T00:00:00Z"
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    assert main(["verify", "--bundle", str(bad_path)]) == 1
    assert "INVALID" in capsys.readouterr().out

    not_json = tmp_path / "x.json"
    not_json.write_text("{")
    assert main(["verify", "--bundle", str(not_json)]) == 1


def test_report_is_one_offline_page(tmp_path, capsys):
    bundle = build_bundle(tmp_path)
    bundle["records"][0]["operator"] = "<script>alert(1)</script>"  # never rendered raw
    path = tmp_path / "b.json"
    path.write_text(json.dumps(bundle))
    out = tmp_path / "r.html"
    assert main(["report", str(path), "-o", str(out)]) == 1  # the edit breaks the record
    page = out.read_text()
    assert "<script" not in page
    assert not re.search(r"(src|href)=|https?://|@import|url\(", page)
    assert "Fails." in page and "What this file does not show" in page

    clean = build_bundle(tmp_path)
    page = render_report_html(clean, check_evidence_file(clean), source="b.json")
    assert "Everything this file claims checks" in page
    assert "The request and response text" in page
    assert "capsule-emit verify --bundle b.json" in page
