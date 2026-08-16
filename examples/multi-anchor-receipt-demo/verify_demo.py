#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Multi-receipt verifier — checks each receipt independently.

Loads a multi-receipt record (JSON produced by run_demo.py --save or a prior
run) and verifies every stored receipt against its logged public key.  Results
are printed per-receipt with no aggregate pass/fail.

Verifier-policy note: this script makes no claim about how many receipts are
sufficient, or which logs count.  Those are relying-party decisions.

Partial-reachability: a receipt marked "absent" (not present in the record,
or flagged as absent) is reported as absent — never as a pass or a fail.

Usage:
    python examples/multi-anchor-receipt-demo/verify_demo.py <record.json>
    python examples/multi-anchor-receipt-demo/verify_demo.py  # uses demo.json
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from agent_action_capsule import verify as capsule_verify


def _raw_ed25519_hex_to_pem(pubkey_hex: str) -> bytes:
    raw = bytes.fromhex(pubkey_hex)
    spki_prefix = bytes.fromhex("302a300506032b6570032100")
    der = spki_prefix + raw
    b64 = base64.encodebytes(der).strip().decode("ascii")
    return (
        b"-----BEGIN PUBLIC KEY-----\n"
        + b64.encode("ascii")
        + b"\n-----END PUBLIC KEY-----\n"
    )


def _verify_receipt_entry(capsule_id: str, entry: dict) -> dict:
    """Verify one receipt entry from the multi-receipt record.

    Entry schema:
      label, ts_url, log_pubkey_hex, entry_hash, leaf_index, tree_size,
      receipt_b64

    Returns a status dict with keys:
      label, ts_url, receipt_present, receipt_ok, leaf_index, tree_size,
      entry_hash, errors
    """
    from scitt_cose import verify_receipt

    label = entry.get("label", "unknown")
    ts_url = entry.get("ts_url", "unknown")

    # A receipt entry with no receipt_b64 is recorded as absent.
    receipt_b64 = entry.get("receipt_b64")
    if not receipt_b64:
        return {
            "label": label,
            "ts_url": ts_url,
            "receipt_present": False,
            "receipt_ok": None,
            "leaf_index": entry.get("leaf_index"),
            "tree_size": entry.get("tree_size"),
            "entry_hash": entry.get("entry_hash"),
            "errors": ["no receipt_b64 in record — absent at submission time"],
        }

    log_pubkey_hex = entry.get("log_pubkey_hex", "")
    if not log_pubkey_hex:
        return {
            "label": label,
            "ts_url": ts_url,
            "receipt_present": True,
            "receipt_ok": False,
            "leaf_index": entry.get("leaf_index"),
            "tree_size": entry.get("tree_size"),
            "entry_hash": entry.get("entry_hash"),
            "errors": ["no log_pubkey_hex — cannot verify receipt signature"],
        }

    entry_hash = entry.get("entry_hash", "")
    leaf_index = entry.get("leaf_index")
    tree_size = entry.get("tree_size")

    # Sanity: for /v1/digest submissions, entry_hash == SHA-256(bytes.fromhex(capsule_id)).
    expected_entry_hash = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()
    if entry_hash != expected_entry_hash:
        return {
            "label": label,
            "ts_url": ts_url,
            "receipt_present": True,
            "receipt_ok": False,
            "leaf_index": leaf_index,
            "tree_size": tree_size,
            "entry_hash": entry_hash,
            "errors": [
                f"entry_hash mismatch: got {entry_hash!r}, "
                f"expected SHA-256(capsule_id_bytes) = {expected_entry_hash!r}"
            ],
        }

    log_pem = _raw_ed25519_hex_to_pem(log_pubkey_hex)
    receipt_bytes = base64.b64decode(receipt_b64)
    result = verify_receipt(
        receipt_bytes,
        leaf_entry_hex=entry_hash,
        log_public_key_pem=log_pem,
    )

    return {
        "label": label,
        "ts_url": ts_url,
        "receipt_present": True,
        "receipt_ok": result.ok,
        "leaf_index": leaf_index,
        "tree_size": tree_size,
        "entry_hash": entry_hash,
        "errors": [str(e) for e in result.errors] if not result.ok else [],
    }


def _print_status(s: dict) -> None:
    label = s["label"]
    ts_url = s["ts_url"]
    present = s["receipt_present"]
    ok = s["receipt_ok"]
    if not present:
        print(f"  [{label}]  ABSENT   ts_url={ts_url}")
        for e in s.get("errors", []):
            print(f"             detail: {e}")
    elif ok:
        print(
            f"  [{label}]  VERIFIED ts_url={ts_url}"
            f"  leaf={s['leaf_index']}  tree_size={s['tree_size']}"
        )
    else:
        print(f"  [{label}]  INVALID  ts_url={ts_url}")
        for e in s.get("errors", []):
            print(f"             detail: {e}")


def verify_record(record_path: Path) -> int:
    """Verify all receipts in a multi-receipt record.  Returns exit code."""
    record = json.loads(record_path.read_text())
    capsule_id: str = record["capsule"]["capsule_id"]
    capsule = record["capsule"]
    receipts: list[dict] = record.get("receipts", [])

    print(f"record : {record_path}")
    print(f"capsule_id : {capsule_id}")
    print()

    # Capsule-level payload verification (Class-1, offline).
    cv = capsule_verify(capsule)
    print(f"capsule payload verify : {'VALID' if cv.ok else 'INVALID'}")
    if not cv.ok:
        for f in cv.findings:
            print(f"  [{f.severity}] {f.code}: {f.detail}")
    print()

    # Per-receipt verification — no aggregate result.
    print("receipt verification (per-anchor, independent):")
    statuses = []
    for entry in receipts:
        s = _verify_receipt_entry(capsule_id, entry)
        _print_status(s)
        statuses.append(s)

    if not receipts:
        print("  (no receipts in record)")

    print()
    print(
        "Verifier policy: how many receipts are required, and from which logs,\n"
        "is a relying-party decision.  This verifier reports each receipt's\n"
        "status individually and makes no policy judgment."
    )

    # Exit 0 only when the capsule payload is valid AND at least one receipt
    # verified.  This is the minimal soundness floor — the policy of "require
    # N receipts from M specific logs" is left to the relying party.
    payload_ok = cv.ok
    any_receipt_ok = any(s["receipt_ok"] for s in statuses)

    return 0 if (payload_ok and any_receipt_ok) else 1


def main() -> None:
    if len(sys.argv) > 1:
        record_path = Path(sys.argv[1])
    else:
        record_path = Path(__file__).parent / "demo.json"
        if not record_path.exists():
            print("No record file specified and demo.json not found.")
            print(f"Usage: {sys.argv[0]} <record.json>")
            sys.exit(1)

    sys.exit(verify_record(record_path))


if __name__ == "__main__":
    main()
