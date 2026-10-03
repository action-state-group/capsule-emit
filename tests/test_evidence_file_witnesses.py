# SPDX-License-Identifier: Apache-2.0
"""The witness receipts an evidence file's checkpoint carries
(``checkpoint.witnesses``), checked against the signed checkpoint under a
witness directory's keys: pass under a known key, withheld when the file
carries none or no key is known, fail when one does not verify. Only a
failing receipt changes the verdict.

The receipts here are real COSE receipts minted over a live evidence file's
signed checkpoint with this suite's test-only witness key
(``_stub_receipt``); the witness at ``WITNESS`` is listed in a directory
under that key."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cll.checkpoint.index")

from _stub_receipt import (  # noqa: E402
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
    load_pem_public_key,
)

from capsule_emit.evidence_file import check_evidence_file, default_witness_directory  # noqa: E402
from capsule_emit.evidence_report import render_report_html  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VECTOR = ROOT / "test-vectors" / "evidence-file" / "two-node-provider.json"
WITNESS = "https://witness.example"


def _directory(endpoint: str = WITNESS) -> dict:
    raw = load_pem_public_key(TEST_TS_PUBLIC_KEY_PEM).public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return {
        "directory_version": "1",
        "witnesses": [{"name": "test witness", "endpoint": endpoint, "since": "2026-01-01", "key_ids": [raw]}],
    }


def _witnessed() -> dict:
    """The live file, its checkpoint carrying one receipt minted over it."""
    bundle = json.loads(VECTOR.read_text())
    cose = bundle["checkpoint"]["cose"]
    signed = checkpoint_dict_from_cose(base64.urlsafe_b64decode(cose + "=" * (-len(cose) % 4)))
    entry_hash = checkpoint_entry_hash(signed)
    bundle["checkpoint"]["witnesses"] = [
        {
            "ts_url": WITNESS,
            "entry_hash": entry_hash,
            "receipt_b64": build_stub_receipt_b64(entry_hash),
            "leaf_index": "0",
            "tree_size": "1",
        }
    ]
    return bundle


def test_a_receipt_verifies_under_a_known_witness_key():
    without = check_evidence_file(json.loads(VECTOR.read_text()), witness_directory=_directory())
    check = check_evidence_file(_witnessed(), witness_directory=_directory())
    assert check.witness.status == "pass"
    assert check.witness.findings == ()
    assert [(r["ts_url"], r["binding"], r["status"]) for r in check.witness_receipts] == [(WITNESS, "cll", "pass")]
    assert check.to_dict()["witnesses"]["status"] == "pass"
    # Every other check is as it was without the receipt.
    assert [(c.name, c.status) for c in check.checks] == [(c.name, c.status) for c in without.checks]
    assert check.verdict == without.verdict


def test_no_known_key_is_withheld_not_failed():
    # The default directory knows only the default witness, not this one.
    check = check_evidence_file(_witnessed())
    assert check.witness.status == "withheld"
    assert check.witness.findings == (f"witness_unverified:{WITNESS}",)
    assert check.witness_receipts[0]["reason"].startswith("not checked")


def test_the_default_directory_is_the_committed_row_for_the_default_witness():
    from cll.checkpoint import emit

    committed = json.loads((ROOT / "witnesses.json").read_text())
    endpoint = emit.DEFAULT_TS_URL.rstrip("/")
    row = next(r for r in committed["witnesses"] if r["endpoint"] == endpoint)
    (default,) = default_witness_directory()["witnesses"]
    assert (default["endpoint"], default["key_ids"]) == (endpoint, row["key_ids"])


def test_a_file_without_receipts_is_judged_as_before():
    check = check_evidence_file(json.loads(VECTOR.read_text()))
    assert check.witness.status == "withheld"
    assert check.witness.findings == ("witness_receipt_absent",)
    assert check.ok == all(c.status != "fail" for c in check.checks)  # withheld never gates


def test_a_receipt_for_another_checkpoint_fails_the_file():
    bundle = _witnessed()
    other = "00" * 32
    bundle["checkpoint"]["witnesses"][0].update(entry_hash=other, receipt_b64=build_stub_receipt_b64(other))
    check = check_evidence_file(bundle, witness_directory=_directory())
    assert check.witness.status == "fail"
    assert check.witness.findings == (f"witness_receipt_invalid:{WITNESS}",)
    assert check.verdict == "INVALID"


def test_a_receipt_with_edited_bytes_fails_the_file():
    bundle = _witnessed()
    receipt = bundle["checkpoint"]["witnesses"][0]
    raw = bytearray(base64.b64decode(receipt["receipt_b64"]))
    raw[-1] ^= 0x01
    receipt["receipt_b64"] = base64.b64encode(bytes(raw)).decode()
    check = check_evidence_file(bundle, witness_directory=_directory())
    assert check.witness.status == "fail"
    assert check.verdict == "INVALID"


def test_a_malformed_receipt_fails_the_file():
    bundle = _witnessed()
    bundle["checkpoint"]["witnesses"].append("not a receipt")
    check = check_evidence_file(bundle, witness_directory=_directory())
    assert check.witness.status == "fail"
    assert "witness_receipt_malformed:1" in check.witness.findings


def test_receipts_are_not_checked_against_an_unsigned_checkpoint():
    bundle = _witnessed()
    del bundle["checkpoint"]["cose"]
    check = check_evidence_file(bundle, witness_directory=_directory())
    assert check.witness.status == "withheld"
    assert check.witness.findings == ("checkpoint_unverified",)


def test_the_cli_and_the_report_show_the_receipt(tmp_path):
    path = tmp_path / "file.json"
    path.write_text(json.dumps(_witnessed()))
    directory = tmp_path / "witnesses.json"
    directory.write_text(json.dumps(_directory()))
    verify = [sys.executable, "-m", "capsule_emit.cli", "verify", "--bundle", str(path)]
    out = subprocess.run([*verify, "--witness-directory", str(directory)], capture_output=True, text=True)
    assert f"{WITNESS}: ok" in out.stdout
    out = subprocess.run([*verify, "--json"], capture_output=True, text=True)
    assert json.loads(out.stdout)["witnesses"]["status"] == "withheld"
    check = check_evidence_file(_witnessed(), witness_directory=_directory())
    page = render_report_html(_witnessed(), check, source=path.name)
    assert "witness.example: verified" in page
