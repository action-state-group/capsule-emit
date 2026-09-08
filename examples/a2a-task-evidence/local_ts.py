# SPDX-License-Identifier: Apache-2.0
"""A hermetic, loopback-only Transparency Service double.

This is NOT the public witness at witness.agentactioncapsule.org, and it is
NOT an independent third party -- it is a same-process HTTP server on
127.0.0.1 with a freshly generated, throwaway Ed25519 keypair, started only
so this example's checkpoint-registration step ("externalRegistration" in
the verifier, §13 of the proposal) has a real endpoint to POST to and a
real COSE Receipt to check offline, with zero external network egress and a
fully deterministic result. Per the proposal's §16 experimental-acceptance
item 7 ("a same-operator service may establish registration under an
accepted key but is labeled producer-operated, and never promoted to
independent continuity"), a caller running this locally must never present
its receipts as independently witnessed -- the demo and the vectors label it
"local-ts (non-independent, demo-only)" everywhere it appears.

It speaks the real wire protocol capsule-emit's witness client uses
(``cll.checkpoint.emit.register_checkpoint``): decode + verify the posted
COSE-wire checkpoint statement, mint a real single-leaf COSE Receipt over
its entry_hash, and return it. No shortcuts, no mock objects -- the same
code path a real Transparency Service exercises, minus the independence.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
from dataclasses import dataclass

from cll.checkpoint.cose_wire import verify_checkpoint_cose_offline
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from scitt_cose import build_receipt

#: Field set a checkpoint's signature covers -- must match
#: ``cll.checkpoint.emit.CheckpointRecord.signing_body()``.
_CHECKPOINT_SIGNING_FIELDS = (
    "v",
    "kind",
    "log_id",
    "mmr_size",
    "root",
    "prev_size",
    "prev_root",
    "key_id",
    "timestamp",
)


def _entry_hash(checkpoint: dict) -> str:
    body = {k: checkpoint[k] for k in _CHECKPOINT_SIGNING_FIELDS}
    signing_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(signing_body).hexdigest()
    return hashlib.sha256(bytes.fromhex(digest)).hexdigest()


@dataclass(frozen=True)
class LocalTransparencyService:
    """A running loopback TS double plus everything a verifier needs to
    check its receipts offline: ``url`` (pass as ``witness_url=`` to
    ``seal()``) and ``public_key_pem`` (pass as this TS's entry in a
    verifier's ``trust_anchor``/``ts_pubkeys`` mapping)."""

    url: str
    public_key_pem: bytes
    _server: http.server.ThreadingHTTPServer
    _thread: threading.Thread

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def start_local_ts(*, host: str = "127.0.0.1", port: int = 0) -> LocalTransparencyService:
    """Start the loopback double and return its handle. Caller must call
    ``.stop()`` when done (or use it as a context manager via ``with
    contextlib.closing``-style code in the caller)."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    public_pem = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # silence per-request logging
            pass

        def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
            if self.path != "/checkpoints":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            result = verify_checkpoint_cose_offline(raw)
            if not result.ok:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"errors": result.errors}).encode())
                return
            checkpoint = result.decoded.to_checkpoint_record().to_dict()
            entry_hash = _entry_hash(checkpoint)
            receipt_bytes = build_receipt(
                leaf_entry_hex=entry_hash,
                leaf_index=0,
                tree_entries_hex=[entry_hash],
                alg="EdDSA",
                log_private_key_pem=private_pem,
            )
            response = {
                "entry_hash": entry_hash,
                "receipt_b64": base64.b64encode(receipt_bytes).decode(),
                "leaf_index": 0,
                "tree_size": 1,
            }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer((host, port), _Handler)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return LocalTransparencyService(
        url=f"http://{host}:{actual_port}",
        public_key_pem=public_pem,
        _server=server,
        _thread=thread,
    )
