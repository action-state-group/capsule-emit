# SPDX-License-Identifier: Apache-2.0
"""Signed peaks-checkpoint emission, TS registration, and offline verification.

A *checkpoint* is a signed, tamper-evident snapshot of one log's MMR peak set:
``{log_id, mmr_size, root, prev_size, prev_root, key_id, timestamp}``.
Registering its digest with a SCITT Transparency Service (TS)
yields a COSE Receipt that provides third-party freshness evidence up to that
checkpoint.

This is the CLL (Checkpointed Local Log) checkpoint shape ratified in
Amendment E: the ``capsule-ledger`` shape shipped in ``ldg-peaks-checkpoint-
emit`` (2026-08-18), plus ``log_id`` -- the field a multi-log or multi-peer
deployment (e.g. a mesh node checkpointing several independent streams) needs
to tell checkpoints apart. ``key_id`` doubles as a peer identifier in that
setting: it is whatever label the signer's own key is registered under, and a
per-peer signing key makes ``key_id`` and "which peer wrote this" the same
fact.

Verification chain (all links must hold):
  1. MMR inclusion-to-peak: any leaf under ``mmr_size`` is genuinely in the
     log (``MmrLedger.inclusion_proof`` + ``core.verify_inclusion``).
  2. Checkpoint signature: the peak set committed at ``mmr_size`` is
     operator-signed (``verify_checkpoint_signature``).
  3. TS receipt: the checkpoint digest appears in the TS's append-only log
     (``verify_receipt_offline`` via scitt-cose).
  4. Rollback detection: the current MMR's root at the *previous*
     checkpoint's size matches the previous checkpoint's stored root
     (``verify_checkpoint_consistency``).

These functions are OPTIONAL and OFF by default when used directly -- nothing
in this module makes a network call unless the caller invokes
``register_checkpoint`` or ``verify_receipt_offline(ts_base_url=...)``. Once
enabled: cadence and max-lag are declared via ``CheckpointConfig``; sizes are
monotonic; peak-consistency with the previous checkpoint is enforced before
each new checkpoint is accepted (``RollbackError``). The operator supplies
their own signing key and their own schedule (a cron, a timer, whatever the
deployment already has) -- timing-jitter or scheduling as an operated service
is explicitly out of scope here.

Since 0.5.0, ``capsule_emit.core.emit()``'s default path drives these same
functions automatically once a ledger forms a checkpoint-worthy stream --
see ``capsule_emit.witness``. That is a caller of this module, not a change
to it: everything above stays true for direct/manual use — nothing here
reaches for a signing key, a schedule, or the network on its own.

The free public-good witness tier lives at ``DEFAULT_TS_URL``
(``anchor.agentactioncapsule.org``) -- prefilled as the config default so a
caller who wants it need not look it up, but never contacted unless
``register_checkpoint``/``verify_receipt_offline`` is actually called, and
freely substitutable with any conforming Transparency Service. A generated
config file should show it commented out (see ``EXAMPLE_CONFIG_TOML``), so
opting in is an explicit uncomment, not a silent default.

The checkpoint ``signature`` covers all fields except itself and
``witnesses`` (deterministic JSON, ``sort_keys=True``); the digest registered
with the TS is ``sha256(signing_body_utf8).hexdigest()`` -- exactly 64 hex
chars, matching the capsule-anchor ``/v1/digest`` endpoint.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .index import MmrLedger

__all__ = [
    "CheckpointConfig",
    "WitnessRecord",
    "CheckpointRecord",
    "Signer",
    "CheckpointError",
    "RollbackError",
    "emit_checkpoint",
    "verify_checkpoint_signature",
    "verify_checkpoint_consistency",
    "register_checkpoint",
    "verify_receipt_offline",
    "due_for_checkpoint",
    "lag_exceeded",
    "DEFAULT_TS_URL",
    "EXAMPLE_CONFIG_TOML",
]

DEFAULT_TS_URL = "https://anchor.agentactioncapsule.org"

#: A generated-config snippet: the witness URL is prefilled with the free
#: public-good tier but shipped COMMENTED OUT, so registration stays opt-in
#: even when a caller copies this block verbatim. Any conforming TS may be
#: substituted for the URL.
EXAMPLE_CONFIG_TOML = f"""\
[checkpoint]
cadence_entries = 100
max_lag_entries = 200
# ts_urls = ["{DEFAULT_TS_URL}"]
"""


class CheckpointError(RuntimeError):
    """A checkpoint operation failed for a non-integrity reason (config, network)."""


class RollbackError(RuntimeError):
    """The current MMR is inconsistent with a prior checkpoint (rollback detected)."""


class Signer(Protocol):
    """Any object with a stable ``key_id`` and a ``sign(digest_hex) -> str``
    method. Never imported concretely by this module -- bring your own key
    management."""

    key_id: str

    def sign(self, digest_hex: str) -> str: ...


@dataclass
class CheckpointConfig:
    """Operator-declared checkpointing policy: cadence, max lag, and which
    Transparency Service(s) to register with. ``ts_urls`` is empty by
    default -- registration is opt-in, never assumed."""

    ts_urls: list[str] = field(default_factory=list)
    cadence_entries: int = 100
    max_lag_entries: int = 200

    def to_dict(self) -> dict:
        return {
            "ts_urls": self.ts_urls,
            "cadence_entries": self.cadence_entries,
            "max_lag_entries": self.max_lag_entries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CheckpointConfig:
        return cls(
            ts_urls=list(d.get("ts_urls", [])),
            cadence_entries=int(d.get("cadence_entries", 100)),
            max_lag_entries=int(d.get("max_lag_entries", 200)),
        )


def due_for_checkpoint(cfg: CheckpointConfig, entries_since_last: int) -> bool:
    """True once ``entries_since_last`` reaches the declared cadence."""
    return entries_since_last >= cfg.cadence_entries


def lag_exceeded(cfg: CheckpointConfig, entries_since_last: int) -> bool:
    """True once ``entries_since_last`` exceeds the declared max lag -- the
    caller's signal that the checkpoint is now overdue, not just due."""
    return entries_since_last > cfg.max_lag_entries


@dataclass
class WitnessRecord:
    """Evidence that a checkpoint's digest was seen by one Transparency Service."""

    ts_url: str
    entry_hash: str  # sha256(bytes.fromhex(checkpoint_digest)).hex() -- TS-derived
    receipt_b64: str  # base64-encoded COSE Receipt (COSE_Sign1, CBOR tag 18)
    leaf_index: int
    tree_size: int

    def to_dict(self) -> dict:
        return {
            "ts_url": self.ts_url,
            "entry_hash": self.entry_hash,
            "receipt_b64": self.receipt_b64,
            "leaf_index": self.leaf_index,
            "tree_size": self.tree_size,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WitnessRecord:
        return cls(
            ts_url=d["ts_url"],
            entry_hash=d["entry_hash"],
            receipt_b64=d["receipt_b64"],
            leaf_index=int(d["leaf_index"]),
            tree_size=int(d["tree_size"]),
        )


@dataclass
class CheckpointRecord:
    """A signed snapshot of one log's MMR peak set at ``mmr_size``.

    ``signature`` covers the signing body (every field below except
    ``signature`` and ``witnesses``, serialised as deterministic JSON).
    ``witnesses`` is populated after registration with one or more
    Transparency Services.
    """

    v: int
    kind: str
    log_id: str
    mmr_size: int
    root: str  # hex: root_from_peaks at mmr_size (32B)
    prev_size: int  # 0 for the first checkpoint
    prev_root: str  # hex root at prev_size; empty string for the first checkpoint
    key_id: str  # signer's key id; doubles as peer id in a multi-peer deployment
    timestamp: str  # ISO 8601 UTC
    signature: str  # hex HMAC-SHA256 (or whatever the signer produces) over signing_body
    witnesses: list[WitnessRecord] = field(default_factory=list)

    def signing_body(self) -> str:
        """Canonical JSON over the fields covered by the signature."""
        body = {
            "v": self.v,
            "kind": self.kind,
            "log_id": self.log_id,
            "mmr_size": self.mmr_size,
            "root": self.root,
            "prev_size": self.prev_size,
            "prev_root": self.prev_root,
            "key_id": self.key_id,
            "timestamp": self.timestamp,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """64-char lowercase hex: sha256 of the signing body (UTF-8 encoded).

        This is what gets registered with the Transparency Service.
        """
        return hashlib.sha256(self.signing_body().encode()).hexdigest()

    def to_dict(self) -> dict:
        d = {
            "v": self.v,
            "kind": self.kind,
            "log_id": self.log_id,
            "mmr_size": self.mmr_size,
            "root": self.root,
            "prev_size": self.prev_size,
            "prev_root": self.prev_root,
            "key_id": self.key_id,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }
        if self.witnesses:
            d["witnesses"] = [w.to_dict() for w in self.witnesses]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CheckpointRecord:
        witnesses = [WitnessRecord.from_dict(w) for w in d.get("witnesses", [])]
        return cls(
            v=int(d["v"]),
            kind=d["kind"],
            log_id=d["log_id"],
            mmr_size=int(d["mmr_size"]),
            root=d["root"],
            prev_size=int(d["prev_size"]),
            prev_root=d.get("prev_root", ""),
            key_id=d["key_id"],
            timestamp=d["timestamp"],
            signature=d["signature"],
            witnesses=witnesses,
        )


# -- signing / verification --------------------------------------------------


def _root_hex(mmr: MmrLedger, size: int) -> str:
    from . import core

    return core.root_from_peaks(mmr.peak_hashes_at(size)).hex()


def emit_checkpoint(
    mmr: MmrLedger,
    signer: Signer,
    *,
    log_id: str,
    prev: CheckpointRecord | None = None,
    timestamp: str | None = None,
) -> CheckpointRecord:
    """Build and sign a checkpoint from ``mmr``'s current state for ``log_id``.

    ``prev`` is the previous checkpoint for this same ``log_id`` (for
    monotonicity + rollback detection). ``timestamp`` overrides the current
    UTC time (for deterministic tests).

    Raises ``RollbackError`` if the MMR is inconsistent with ``prev``, or
    ``CheckpointError`` if ``prev`` belongs to a different log.
    """
    if timestamp is None:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    current_size = mmr.size()
    if current_size == 0:
        raise CheckpointError("cannot checkpoint an empty MMR (no leaves appended yet)")

    prev_size = 0
    prev_root = ""

    if prev is not None:
        if prev.log_id != log_id:
            raise CheckpointError(
                f"prev checkpoint belongs to log_id={prev.log_id!r}, not {log_id!r}"
            )
        if current_size <= prev.mmr_size:
            raise RollbackError(
                f"MMR size {current_size} is not greater than previous checkpoint size "
                f"{prev.mmr_size} -- monotonicity violated"
            )
        # Rollback detection: the current MMR's root at prev_size must match.
        actual_prev_root = _root_hex(mmr, prev.mmr_size)
        if actual_prev_root != prev.root:
            raise RollbackError(
                f"MMR root at prev_size={prev.mmr_size} is {actual_prev_root!r} "
                f"but prior checkpoint recorded {prev.root!r} -- log has been mutated"
            )
        prev_size = prev.mmr_size
        prev_root = prev.root

    root_hex = _root_hex(mmr, current_size)

    # Build unsigned record so we can compute the signing body.
    cp = CheckpointRecord(
        v=1,
        kind="mmr_checkpoint",
        log_id=log_id,
        mmr_size=current_size,
        root=root_hex,
        prev_size=prev_size,
        prev_root=prev_root,
        key_id=signer.key_id,
        timestamp=timestamp,
        signature="",
    )
    sig = signer.sign(cp.digest())
    cp.signature = sig
    return cp


def verify_checkpoint_signature(cp: CheckpointRecord, signer: Signer) -> bool:
    """Recompute and compare the checkpoint's signature. Never raises."""
    try:
        expected = signer.sign(cp.digest())
        return cp.signature == expected
    except Exception:
        return False


def verify_checkpoint_consistency(
    prev: CheckpointRecord, current: CheckpointRecord, mmr: MmrLedger
) -> bool:
    """Check that ``current`` extends ``prev`` (same ``log_id``) without rollback.

    Recomputes the MMR's root at ``prev.mmr_size`` from the live node store
    and compares it against ``current.prev_root``. A mutated or rolled-back
    log produces a different root.
    """
    try:
        if current.log_id != prev.log_id:
            return False
        if current.prev_size != prev.mmr_size:
            return False
        if current.mmr_size <= prev.mmr_size:
            return False
        actual = _root_hex(mmr, prev.mmr_size)
        return actual == current.prev_root
    except Exception:
        return False


# -- TS registration -----------------------------------------------------


def register_checkpoint(
    cp: CheckpointRecord,
    ts_url: str = DEFAULT_TS_URL,
    *,
    timeout: float = 30.0,
) -> WitnessRecord:
    """POST the checkpoint digest to ``ts_url/v1/digest`` and return a WitnessRecord.

    The TS returns a COSE Receipt over the checkpoint's digest. The receipt
    proves that this checkpoint was seen by the TS at some point in its log.
    Never called implicitly -- registration is always the caller's decision.
    """
    digest = cp.digest()
    url = ts_url.rstrip("/") + "/v1/digest"
    payload = json.dumps({"capsule_id": digest}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise CheckpointError(f"TS returned HTTP {exc.code}: {detail}") from exc

    return WitnessRecord(
        ts_url=ts_url,
        entry_hash=body["entry_hash"],
        receipt_b64=body["receipt_b64"],
        leaf_index=int(body["leaf_index"]),
        tree_size=int(body["tree_size"]),
    )


def _fetch_ts_authority_pubkey(ts_base_url: str, *, timeout: float = 15.0) -> bytes:
    """Fetch the raw 32-byte Ed25519 public key from the TS authority-pubkey endpoint."""
    url = ts_base_url.rstrip("/") + "/anchor/authority-pubkey"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    hex_key = body["pubkey_hex"]
    return bytes.fromhex(hex_key)


def _raw_ed25519_to_pem(raw: bytes) -> bytes:
    """Convert a raw 32-byte Ed25519 public key to SubjectPublicKeyInfo PEM."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PublicKey.from_public_bytes(raw)
    return key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def verify_receipt_offline(
    witness: WitnessRecord,
    *,
    ts_pubkey_pem: bytes | str | None = None,
    ts_base_url: str | None = None,
    timeout: float = 15.0,
) -> tuple[bool, list[str]]:
    """Verify a COSE Receipt offline (no network unless the pubkey must be fetched).

    Provide exactly one of ``ts_pubkey_pem`` (cached PEM bytes/str) or
    ``ts_base_url`` (fetches the key once; requires network and the
    ``cryptography`` package). Returns ``(ok, errors)`` -- never raises.
    """
    try:
        from scitt_cose import verify_receipt
    except ImportError:
        return False, ["scitt-cose is not installed; run: pip install 'capsule-emit[checkpoint]'"]

    try:
        import base64

        if ts_pubkey_pem is None:
            if ts_base_url is None:
                ts_base_url = witness.ts_url
            raw = _fetch_ts_authority_pubkey(ts_base_url, timeout=timeout)
            ts_pubkey_pem = _raw_ed25519_to_pem(raw)

        receipt_bytes = base64.b64decode(witness.receipt_b64)
        result = verify_receipt(
            receipt_bytes,
            leaf_entry_hex=witness.entry_hash,
            log_public_key_pem=ts_pubkey_pem,
        )
        return result.ok, result.errors
    except Exception as exc:
        return False, [str(exc)]
