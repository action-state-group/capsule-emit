#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Multi-anchor receipt demo — one record, two SCITT receipts, verified separately.

Demonstrates:
  - A single capsule submitted to two independently-operated transparency logs.
  - A verifier that checks both receipts separately and never aggregates them.
  - The partial-reachability case: one anchor unreachable renders as "absent",
    not as a combined pass or fail.

Verifier-policy note: how many receipts a relying party requires, and from
which logs, is a relying-party decision — it is not encoded in the capsule
format.  This demo has no opinion on that; it shows the mechanism.

Limitation (stated plainly): both transparency logs in this demo are operated
by the same party running the demo.  The mechanism is demonstrated; operator
independence is not.

Usage:
    pip install "capsule-emit[dev]" capsule-anchor scitt-cose
    python examples/multi-anchor-receipt-demo/run_demo.py

    # Run only the partial-reachability scenario:
    python examples/multi-anchor-receipt-demo/run_demo.py --partial
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from scitt_cose import verify_receipt

from capsule_emit import seal
from capsule_emit.verification import verify_capsule as capsule_verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_anchor(port: int, name: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "CAPSULE_ANCHOR_INSECURE_EPHEMERAL_KEY": "1",
        "CAPSULE_ANCHOR_INSECURE_IN_MEMORY": "1",
    }
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "capsule_anchor.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "error",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_ready(base_url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _fetch_log_pubkey_hex(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url}/anchor/authority-pubkey", timeout=10) as r:
        return json.loads(r.read())["pubkey_hex"]


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


def _submit_digest(base_url: str, capsule_id: str) -> dict:
    """POST /v1/digest; return full response dict."""
    payload = json.dumps({"capsule_id": capsule_id}).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/digest",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _try_submit_digest(base_url: str, capsule_id: str) -> dict | None:
    """Submit; return None on any network error (unreachable anchor)."""
    try:
        return _submit_digest(base_url, capsule_id)
    except (urllib.error.URLError, OSError):
        return None


def _verify_one_receipt(
    anchor_label: str,
    capsule_id: str,
    submission: dict | None,
    log_pubkey_pem: bytes | None,
) -> dict:
    """Verify a single receipt and return a status dict.

    Returns a dict with:
      label, receipt_present, receipt_ok, leaf_index, tree_size, entry_hash
    """
    if submission is None or log_pubkey_pem is None:
        return {
            "label": anchor_label,
            "receipt_present": False,
            "receipt_ok": None,
            "leaf_index": None,
            "tree_size": None,
            "entry_hash": None,
            "errors": ["anchor unreachable — no receipt obtained"],
        }

    receipt_b64: str = submission["receipt_b64"]
    entry_hash: str = submission["entry_hash"]
    leaf_index: int = submission["leaf_index"]
    tree_size: int = submission["tree_size"]

    # Sanity: entry_hash for /v1/digest is SHA-256 of the raw capsule_id bytes.
    expected_entry_hash = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()
    if entry_hash != expected_entry_hash:
        return {
            "label": anchor_label,
            "receipt_present": True,
            "receipt_ok": False,
            "leaf_index": leaf_index,
            "tree_size": tree_size,
            "entry_hash": entry_hash,
            "errors": [
                f"entry_hash mismatch: got {entry_hash!r}, "
                f"expected {expected_entry_hash!r}"
            ],
        }

    receipt_bytes = base64.b64decode(receipt_b64)
    result = verify_receipt(
        receipt_bytes,
        leaf_entry_hex=entry_hash,
        log_public_key_pem=log_pubkey_pem,
    )

    return {
        "label": anchor_label,
        "receipt_present": True,
        "receipt_ok": result.ok,
        "leaf_index": leaf_index,
        "tree_size": tree_size,
        "entry_hash": entry_hash,
        "errors": [str(e) for e in result.errors] if not result.ok else [],
    }


def _print_receipt_status(status: dict) -> None:
    label = status["label"]
    present = status["receipt_present"]
    ok = status["receipt_ok"]
    if not present:
        print(f"  [{label}]  ABSENT — {status['errors'][0]}")
    elif ok:
        print(
            f"  [{label}]  VERIFIED"
            f"  leaf_index={status['leaf_index']}"
            f"  tree_size={status['tree_size']}"
        )
    else:
        errs = "; ".join(status["errors"])
        print(f"  [{label}]  INVALID — {errs}")


def _section(title: str) -> None:
    print(f"\n─── {title} " + "─" * max(0, 66 - len(title)))


# ---------------------------------------------------------------------------
# Main scenarios
# ---------------------------------------------------------------------------

def run_full_demo(ledger: Path) -> None:
    """Both anchors up — one record, two receipts, both verified."""
    port_a = _find_free_port()
    port_b = _find_free_port()
    url_a = f"http://127.0.0.1:{port_a}"
    url_b = f"http://127.0.0.1:{port_b}"

    print(f"  anchor-A  {url_a}")
    print(f"  anchor-B  {url_b}")
    print("  (both run in-memory with ephemeral keys — demo-grade only)")

    proc_a = _start_anchor(port_a, "anchor-A")
    proc_b = _start_anchor(port_b, "anchor-B")
    try:
        _section("Step 1 — wait for both anchors to be ready")
        ok_a = _wait_ready(url_a)
        ok_b = _wait_ready(url_b)
        if not ok_a or not ok_b:
            print("ERROR: one or both anchors did not start in time")
            sys.exit(1)
        print("  anchor-A  ready")
        print("  anchor-B  ready")

        # Fetch both log public keys now so we can verify receipts offline later.
        pubkey_hex_a = _fetch_log_pubkey_hex(url_a)
        pubkey_hex_b = _fetch_log_pubkey_hex(url_b)
        log_pem_a = _raw_ed25519_hex_to_pem(pubkey_hex_a)
        log_pem_b = _raw_ed25519_hex_to_pem(pubkey_hex_b)

        _section("Step 2 — seal one capsule")
        result = seal(
            {"query": "demonstrate multi-anchor receipts"},
            action="multi_anchor_demo",
            operator="demo-operator",
            developer="demo-agent@v1",
            agent_output={"status": "recorded"},
            model={"provider": "demo", "model_id": "demo-model"},
            verdict="executed",
            effect={"type": "multi_anchor_demo", "status": "dispatched"},
            anchor=False,
            ledger=ledger,
        )
        capsule_id = result.capsule_id
        print(f"  capsule_id : {capsule_id}")
        capsule_verify_result = capsule_verify(result.capsule)
        print(f"  verify().ok: {capsule_verify_result.ok}")
        assert capsule_verify_result.ok

        _section("Step 3 — submit to anchor-A")
        sub_a = _submit_digest(url_a, capsule_id)
        print(f"  entry_hash  : {sub_a['entry_hash']}")
        print(f"  leaf_index  : {sub_a['leaf_index']}")
        print(f"  tree_size   : {sub_a['tree_size']}")

        _section("Step 4 — submit to anchor-B")
        sub_b = _submit_digest(url_b, capsule_id)
        print(f"  entry_hash  : {sub_b['entry_hash']}")
        print(f"  leaf_index  : {sub_b['leaf_index']}")
        print(f"  tree_size   : {sub_b['tree_size']}")

        _section("Step 5 — verify both receipts independently (never aggregated)")
        status_a = _verify_one_receipt("anchor-A", capsule_id, sub_a, log_pem_a)
        status_b = _verify_one_receipt("anchor-B", capsule_id, sub_b, log_pem_b)
        _print_receipt_status(status_a)
        _print_receipt_status(status_b)

        assert status_a["receipt_ok"], f"anchor-A receipt did not verify: {status_a}"
        assert status_b["receipt_ok"], f"anchor-B receipt did not verify: {status_b}"

        _section("Step 6 — save multi-receipt record")
        record = {
            "capsule_id": capsule_id,
            "capsule": result.capsule,
            "receipts": [
                {
                    "label": "anchor-A",
                    "ts_url": url_a,
                    "log_pubkey_hex": pubkey_hex_a,
                    "entry_hash": sub_a["entry_hash"],
                    "leaf_index": sub_a["leaf_index"],
                    "tree_size": sub_a["tree_size"],
                    "receipt_b64": sub_a["receipt_b64"],
                },
                {
                    "label": "anchor-B",
                    "ts_url": url_b,
                    "log_pubkey_hex": pubkey_hex_b,
                    "entry_hash": sub_b["entry_hash"],
                    "leaf_index": sub_b["leaf_index"],
                    "tree_size": sub_b["tree_size"],
                    "receipt_b64": sub_b["receipt_b64"],
                },
            ],
        }
        record_path = Path(tempfile.gettempdir()) / "multi_receipt_record.json"
        record_path.write_text(json.dumps(record, indent=2))
        print(f"  saved to: {record_path}")

    finally:
        proc_a.terminate()
        proc_b.terminate()
        proc_a.wait()
        proc_b.wait()


def run_partial_demo(ledger: Path) -> None:
    """Anchor-B down — receipt-A present and verified, receipt-B absent.

    The verifier reports each status separately.  There is no aggregate result.
    """
    port_a = _find_free_port()
    port_b = _find_free_port()  # will never start
    url_a = f"http://127.0.0.1:{port_a}"
    url_b = f"http://127.0.0.1:{port_b}"

    print(f"  anchor-A  {url_a}  (running)")
    print(f"  anchor-B  {url_b}  (not started — simulating unavailable anchor)")

    proc_a = _start_anchor(port_a, "anchor-A")
    try:
        _section("Step 1 — wait for anchor-A; anchor-B intentionally absent")
        ok_a = _wait_ready(url_a)
        if not ok_a:
            print("ERROR: anchor-A did not start in time")
            sys.exit(1)
        print("  anchor-A  ready")
        print("  anchor-B  not started")

        pubkey_hex_a = _fetch_log_pubkey_hex(url_a)
        log_pem_a = _raw_ed25519_hex_to_pem(pubkey_hex_a)

        _section("Step 2 — seal capsule")
        result = seal(
            {"query": "partial reachability scenario"},
            action="multi_anchor_demo_partial",
            operator="demo-operator",
            developer="demo-agent@v1",
            agent_output={"status": "recorded"},
            model={"provider": "demo", "model_id": "demo-model"},
            verdict="executed",
            effect={"type": "multi_anchor_demo_partial", "status": "dispatched"},
            anchor=False,
            ledger=ledger,
        )
        capsule_id = result.capsule_id
        print(f"  capsule_id : {capsule_id}")

        _section("Step 3 — submit to anchor-A (succeeds)")
        sub_a = _submit_digest(url_a, capsule_id)
        print(f"  anchor-A  entry_hash : {sub_a['entry_hash']}")

        _section("Step 4 — submit to anchor-B (anchor down → absent)")
        sub_b = _try_submit_digest(url_b, capsule_id)
        print(f"  anchor-B  result: {'response received' if sub_b else 'connection refused (unreachable)'}")

        _section("Step 5 — verify receipts separately")
        status_a = _verify_one_receipt("anchor-A", capsule_id, sub_a, log_pem_a)
        status_b = _verify_one_receipt("anchor-B", capsule_id, sub_b, None)
        _print_receipt_status(status_a)
        _print_receipt_status(status_b)

        # There is no aggregate "all-pass" check.  Each receipt stands alone.
        assert status_a["receipt_ok"], "anchor-A receipt did not verify"
        assert not status_b["receipt_present"], "anchor-B receipt should be absent"

        print()
        print("  Partial-reachability result: anchor-A verified; anchor-B absent.")
        print("  How many receipts are required, and from whom, is a relying-party")
        print("  decision — not a property of this record.")

    finally:
        proc_a.terminate()
        proc_a.wait()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--partial", action="store_true",
        help="Run only the partial-reachability scenario (anchor-B down)",
    )
    args = parser.parse_args()

    print("=== multi-anchor receipt demo ===\n")
    print(
        "LIMITATION: both transparency logs in this demo are operated by the\n"
        "same party running the demo.  The mechanism is demonstrated; operator\n"
        "independence is not.\n"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Path(tmpdir) / "ledger.jsonl"
        if args.partial:
            _section("Partial-reachability scenario (anchor-B intentionally down)")
            run_partial_demo(ledger)
        else:
            _section("Full run (both anchors up)")
            run_full_demo(ledger)
            print()
            _section("Partial-reachability scenario (anchor-B intentionally down)")
            run_partial_demo(ledger)

    print()
    print("=== demo complete ===")


if __name__ == "__main__":
    main()
