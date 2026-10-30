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
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("cll.checkpoint.index")

from _stub_receipt import (  # noqa: E402
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_public_key,
)

from capsule_emit.evidence_file import check_evidence_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
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


def test_no_directory_trusts_no_witness():
    # No witness is privileged: without a directory nothing is checked.
    check = check_evidence_file(_witnessed())
    assert check.witness.status == "withheld"
    assert check.witness.findings == (f"witness_unverified:{WITNESS}",)
    assert check.witness_receipts[0]["reason"].startswith("not checked")


def test_a_directory_without_a_row_for_the_witness_is_withheld():
    directory = _directory("https://other.example")
    check = check_evidence_file(_witnessed(), witness_directory=directory)
    assert check.witness.status == "withheld"
    assert check.witness.findings == (f"witness_unverified:{WITNESS}",)
    assert check.witness_receipts[0]["reason"] == "not checked: no directory row for this witness"


def test_a_row_with_no_keys_is_withheld_not_failed():
    directory = _directory()
    directory["witnesses"][0]["key_ids"] = []
    check = check_evidence_file(_witnessed(), witness_directory=directory)
    assert check.witness.status == "withheld"
    assert check.witness.findings == (f"witness_unverified:{WITNESS}",)
    assert check.witness_receipts[0]["reason"] == "not checked: no key in the directory row for this witness"
    assert check.verdict == check_evidence_file(json.loads(VECTOR.read_text())).verdict


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


def test_the_cli_shows_the_receipt(tmp_path):
    path = tmp_path / "file.json"
    path.write_text(json.dumps(_witnessed()))
    directory = tmp_path / "witnesses.json"
    directory.write_text(json.dumps(_directory()))
    verify = [sys.executable, "-m", "capsule_emit.cli", "verify", "--bundle", str(path)]
    out = subprocess.run([*verify, "--witness-directory", str(directory)], capture_output=True, text=True)
    assert f"{WITNESS}: ok" in out.stdout
    out = subprocess.run([*verify, "--json"], capture_output=True, text=True)
    assert json.loads(out.stdout)["witnesses"]["status"] == "withheld"


# --- the checked registration time (agent-action-capsule#392, capsule-emit#363) ---


def _with_iat(bundle: dict, iat: int | None, *, key_pem: bytes | None = None) -> dict:
    """Replace the receipt with one whose protected CWT claims carry ``iat``."""
    from _stub_receipt import TEST_TS_PRIVATE_KEY_PEM
    from scitt_cose import build_receipt

    receipt = bundle["checkpoint"]["witnesses"][0]
    receipt["receipt_b64"] = base64.b64encode(
        build_receipt(
            leaf_entry_hex=receipt["entry_hash"],
            leaf_index=0,
            tree_entries_hex=[receipt["entry_hash"]],
            alg="EdDSA",
            log_private_key_pem=key_pem or TEST_TS_PRIVATE_KEY_PEM,
            iat=iat,
        )
    ).decode()
    return bundle


def _times(check) -> list[tuple]:
    return [(r["registration_time"], r["registration_time_reason"]) for r in check.witness_receipts]


def test_a_passing_receipt_reports_the_iat_the_witness_signed():
    bundle = _witnessed()
    # 90 seconds after the checkpoint: the time comes from the receipt, never the checkpoint.
    stamp = json.loads(VECTOR.read_text())["checkpoint"]["timestamp"]
    iat = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()) + 90
    check = check_evidence_file(_with_iat(bundle, iat), witness_directory=_directory())
    assert check.witness.status == "pass"
    assert _times(check) == [(iat, None)]
    assert check.to_dict()["witnesses"]["receipts"][0]["registration_time"] == iat


def test_a_receipt_without_an_iat_names_why():
    check = check_evidence_file(_witnessed(), witness_directory=_directory())
    assert check.witness.status == "pass"
    assert _times(check) == [(None, "iat_absent")]


def test_a_receipt_that_did_not_pass_reports_no_time():
    withheld = check_evidence_file(_with_iat(_witnessed(), 1_700_000_000))
    assert withheld.witness.status == "withheld"
    assert _times(withheld) == [(None, "receipt_not_passed")]

    other_key = Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    wrong = check_evidence_file(
        _with_iat(_witnessed(), 1_700_000_000, key_pem=other_key), witness_directory=_directory()
    )
    assert wrong.verdict == "INVALID"
    assert _times(wrong) == [(None, "receipt_not_passed")]


def test_verify_bundle_prints_the_time_and_the_json_carries_it(tmp_path, capsys):
    from capsule_emit.cli import main

    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(_with_iat(_witnessed(), 1_700_000_000)))
    directory = tmp_path / "dir.json"
    directory.write_text(json.dumps(_directory()))
    args = ["verify", "--bundle", str(bundle), "--witness-directory", str(directory)]
    main(args)
    out = capsys.readouterr().out
    assert "witness-signed time 2023-11-14T22:13:20Z (not checked against the checkpoint or now)" in out
    main([*args, "--json"])
    receipts = json.loads(capsys.readouterr().out)["witnesses"]["receipts"]
    assert receipts[0]["registration_time"] == 1_700_000_000 and receipts[0]["registration_time_reason"] is None


def test_rekor_receipt_line_does_not_say_no_registration_time():
    from capsule_emit.cli import _registration_time_line

    line = _registration_time_line(
        {"binding": "rekor", "registration_time": None, "registration_time_reason": "binding_without_iat"}
    )
    assert line == "Rekor entry: time not reported (binding_without_iat)"
