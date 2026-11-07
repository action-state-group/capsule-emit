# SPDX-License-Identifier: Apache-2.0
"""A Result from a store-backed ledger reaches the bundle with its document
(capsule-emit#443): ``append_result`` into a ``Ledger`` retains the ``payload``
original, and ``build_store_bundle`` carries it as the root's disclosed
``agent_input`` when the caller names the record. Records are
``test-vectors/capsulectl-result/``, as capsulectl wrote them."""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from pathlib import Path

import pytest
from agent_action_capsule.bundle import verify_bundle
from agent_action_capsule.producer_envelope import verify_producer_envelope
from cll.checkpoint.runner import CheckpointRunner, Ed25519CheckpointSigner
from cll.log import sqlite as cll_sqlite
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capsule_emit.artifact.record import PRESENT, Artifact, decode_record
from capsule_emit.artifact.sqlite import SqliteArtifactStore
from capsule_emit.evidence_bundle import MeshLedgerError, build_store_bundle
from capsule_emit.result import ResultRefused, append_result
from capsule_emit.store_backed_ledger import Ledger

VECTORS = Path(__file__).resolve().parents[2] / "test-vectors" / "capsulectl-result"
LOG_ID = "result-store-bundle"
CHECKPOINT_KEY = Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)


def _record(name: str):
    return decode_record((VECTORS / f"{name}.record.json").read_text(encoding="utf-8"))


def _open(tmp_path):
    result = _record("result")
    signer = verify_producer_envelope(result.capsule_id, bytes(result.producer_envelope)).public_key
    db = tmp_path / "ledger.db"
    cll_sqlite.init(db, LOG_ID)
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False, timeout=5)
    conn.execute("PRAGMA foreign_keys=ON")
    artifacts = SqliteArtifactStore(conn, "result-ns", [signer])
    artifacts.init()
    log = cll_sqlite.open(db, LOG_ID)
    ledger, _ = Ledger.open_stores(log, artifacts, linear=False, require_statements=False)
    return ledger, log, artifacts


@pytest.fixture
def stores(tmp_path):
    ledger, log, artifacts = _open(tmp_path)
    for name in ("day-1", "day-2"):
        ledger.append_record(dataclasses.replace(_record(name), artifacts=[]))
    return ledger, log, artifacts


def _checkpoint(log) -> None:
    assert CheckpointRunner(log, Ed25519CheckpointSigner(CHECKPOINT_KEY), LOG_ID, cadence_entries=1).run_once()


def test_the_result_original_is_retained_and_the_bundle_discloses_it_when_named(stores):
    ledger, log, artifacts = stores
    result = _record("result")
    done = append_result(result, ledger)
    assert (done.seq, done.appended) == (3, True)
    held = artifacts.get(result.capsule_id)
    assert [(a.name, a.state) for a in held.artifacts] == [("payload", "present")]
    assert held.artifacts[0].content == result.artifacts[0].content
    _checkpoint(log)

    bundle = build_store_bundle(
        log, artifacts, root=result.capsule_id, interval_from=1, closure_depth=1, reveal=[result.capsule_id]
    )
    verified = verify_bundle(bundle, strict=True)
    assert verified.verdict == "VALID"
    shown = bundle["disclosures"][result.capsule_id]["agent_input"]
    assert shown["result_version"] == "evidence-result-v0"
    assert [(d.member, d.status) for d in verified.disclosures if d.capsule_id == result.capsule_id] == [
        ("agent_input", "disclosure_match")
    ]

    plain = build_store_bundle(log, artifacts, root=result.capsule_id, interval_from=1, closure_depth=1)
    assert "disclosures" not in plain or not plain["disclosures"]
    assert [d.status for d in verify_bundle(plain, strict=True).disclosures if d.capsule_id == result.capsule_id] == [
        "withheld"
    ]


def test_the_original_survives_a_reopen(tmp_path, stores):
    ledger, log, artifacts = stores
    result = _record("result")
    append_result(result, ledger)
    reopened, report = Ledger.open_stores(log, artifacts, linear=False, require_statements=False)
    assert report.valid_entries == 3
    assert append_result(result, reopened).appended is False


def test_appending_again_writes_nothing_and_a_different_record_under_the_id_is_refused(stores):
    ledger, log, artifacts = stores
    result = _record("result")
    assert append_result(result, ledger).appended
    again = append_result(result, ledger)
    assert (again.seq, again.appended) == (3, False)
    assert len(list(log.scan_entries(0, 100))) == 3
    with pytest.raises(ResultRefused, match="different record"):
        note = Artifact("note", PRESENT, "", b"x", hashlib.sha256(b"x").hexdigest())
        append_result(dataclasses.replace(result, artifacts=[*result.artifacts, note]), ledger)


def test_refuses_a_result_citing_a_record_the_ledger_lacks(tmp_path):
    ledger, log, _ = _open(tmp_path)
    ledger.append_record(dataclasses.replace(_record("day-1"), artifacts=[]))
    with pytest.raises(ResultRefused, match="does not hold 1 record"):
        append_result(_record("result"), ledger)
    assert len(list(log.scan_entries(0, 100))) == 1


def test_a_ledger_that_requires_statements_refuses_a_result_with_none(tmp_path):
    ledger, log, artifacts = _open(tmp_path)
    for name in ("day-1", "day-2"):
        ledger.append_record(dataclasses.replace(_record(name), artifacts=[]))
    strict, _ = Ledger.open_stores(log, artifacts, linear=False, require_statements=False)
    strict._require_statements = True
    with pytest.raises(ResultRefused, match="signed-statement"):
        append_result(_record("result"), strict)


def test_revealing_a_record_with_no_retained_original_builds_nothing(stores):
    ledger, log, artifacts = stores
    _checkpoint(log)
    day_1 = _record("day-1").capsule_id
    with pytest.raises(MeshLedgerError, match="no retained payload original"):
        build_store_bundle(log, artifacts, root=day_1, interval_from=1, reveal=[day_1])
