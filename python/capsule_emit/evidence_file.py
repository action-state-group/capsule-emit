# SPDX-License-Identifier: Apache-2.0
"""Check an evidence file: an AAC Evidence Bundle (``evidence-bundle/v2``,
draft-mih-zhang-agent-disclosure-bundle-00) saved by any producer.

Offline. Every check is agent-action-capsule's strict
``verify_bundle(bundle, strict=True, ...)``; this module reads the file,
calls it, and reports the result in plain sentences:

* every record's ``capsule_id`` recomputes, citation closure as declared,
  interval coverage under the checkpoint's range root, each record's
  inclusion proof, and each member of the ``disclosures`` overlay when the
  file carries one (a member that does not match the digest its record
  commits to fails the file, as ``disclosures``);
* the producer signature on every record (vintage ``jcs-n`` records
  included).

The checkpoint is read only from its signed COSE form (``checkpoint.cose``):
its log, size, root, key and time are taken from the verified statement, and
a JSON copy that differs from it fails the file. A file with no checkpoint
signature proves nothing about the log: anyone can rebuild a log over any
subset of records, so its coverage and membership claims are reported as not
shown and the file is never a plain VALID.

It also states, without gating on it, whether every record names the signed
checkpoint's key: the log's own key vouching for records that the log's
owner signed. A key says who holds it, not who that is.

Witness receipts the checkpoint carries (``checkpoint.witnesses``) are
checked against the signed checkpoint under the keys of a witness directory
(a parsed ``witnesses.json``, see :mod:`capsule_emit.witness_directory`) the
verifier supplies; no witness is
trusted by default, so without one no receipt is checked. The result is
reported as ``witnesses``: ``pass`` when a receipt verifies under a key in
the directory, ``withheld`` when the file carries none or the directory has
no row or no key for its witness, ``fail`` when one does not verify. The draft defines no witness member, so
only a failing receipt changes the verdict (to INVALID); a file without one
is judged as before.

Public API
----------
check_evidence_file(bundle, *, require_signature=False, witness_directory=None) -> EvidenceFileCheck
load_evidence_file(path) -> dict
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Check",
    "EvidenceFileCheck",
    "RecordCheck",
    "check_evidence_file",
    "load_evidence_file",
]

# One plain sentence per claim status; `verify --bundle` and capsule-viewer's
# evidence report both read these.
_PLAIN: dict[str, dict[str, str]] = {
    "checkpoint": {
        "pass": "The log's checkpoint is signed, and the file's copy of it matches what was signed.",
        "withheld": "The file carries no signed checkpoint, so nothing ties these records to a log anyone committed to.",
        "fail": "The log's checkpoint signature does not check, or the file's copy differs from what was signed.",
    },
    "records": {
        "pass": "Every record is intact: its id recomputes from its content.",
        "fail": "At least one record does not match its own id.",
    },
    "signatures": {
        "pass": "Every record is signed, and every signature checks.",
        "withheld": "Some records carry no signature; the rest check.",
        "fail": "At least one record's signature does not check.",
    },
    "graph_closure": {
        "pass": "Every record the file's records cite, to the stated depth, is in the file.",
        "withheld": "Some cited records are not in the file; the file says so and lists them.",
        "fail": "The file's citations do not add up.",
    },
    "interval_coverage": {
        "pass": "The records are one unbroken stretch of the log under the signed checkpoint.",
        "withheld": "Not proven that these records are one unbroken stretch of a committed log.",
        "fail": "The proof that these records are one unbroken stretch of the log does not check.",
    },
    "witnesses": {
        "pass": "A witness's receipt shows the checkpoint was registered with it, under a key this verifier knows.",
        "withheld": "No witness receipt is checked: the file carries none, or no key is known for its witness.",
        "fail": "A witness receipt in the file does not verify against the signed checkpoint.",
    },
    "disclosures": {
        "pass": "Every disclosed request or response matches the digest its record commits to.",
        "fail": "At least one disclosed request or response does not match what its record commits to.",
    },
    "per_record_membership": {
        "pass": "Each record is proven to be in the log at the position it claims.",
        "withheld": "Not proven that each record is in a committed log.",
        "fail": "At least one record's proof of being in the log does not check.",
    },
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # pass | withheld | fail
    findings: tuple[str, ...] = ()

    @property
    def plain(self) -> str:
        if "selection_not_checkable" in self.findings:
            return (
                "Producer-selected file; this verifier cannot check its selection. Each record's own "
                "place in the log is checked below."
            )
        if "interval_not_claimed" in self.findings:
            return (
                "Not claimed: the producer selected these records, so nothing is said about the "
                "records between them. Each record's own place in the log is checked below."
            )
        return _PLAIN.get(self.name, {}).get(self.status, self.status)


@dataclass(frozen=True)
class RecordCheck:
    capsule_id: str
    identity_ok: bool
    signature: str  # authored | unclaimed | invalid
    key_id: str | None
    seq: int | None
    messages: tuple[str, ...] = ()


@dataclass
class EvidenceFileCheck:
    kind_ok: bool
    bundle_digest: str | None
    root: str | None
    checks: list[Check] = field(default_factory=list)
    records: list[RecordCheck] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    """The signed checkpoint's own fields (log_id, mmr_size, root, key_id,
    timestamp, ...) when it verified; else empty. Never the unsigned JSON copy."""
    checkpoint_authenticated: bool = False
    signer_matches_checkpoint: bool | None = None
    missing: tuple[str, ...] = ()
    closure_depth: int | None = None
    countersignatures: int = 0
    extensions: tuple[str, ...] = ()
    witness: Check | None = None
    """The witness-receipt check. Outside ``checks``: only a failing receipt
    gates the verdict (see the module docstring)."""
    witness_receipts: list[dict[str, Any]] = field(default_factory=list)
    """One entry per receipt: ``ts_url``, ``binding``, ``status``, ``reason``,
    and AAC's checked ``registration_time`` (the ``iat`` the witness signed, in
    seconds since the epoch, or ``None``) with ``registration_time_reason``
    (why it is ``None``). Nothing here compares it with the checkpoint's
    ``timestamp`` or the current time."""

    @property
    def ok(self) -> bool:
        """No check failed. ``withheld`` is not a failure: the file says what
        it does not prove, and the verifier's output says it too."""
        return (
            self.kind_ok
            and all(c.status != "fail" for c in self.checks)
            and (self.witness is None or self.witness.status != "fail")
        )

    @property
    def proven(self) -> bool:
        """Every check passed outright, nothing withheld."""
        return self.ok and all(c.status == "pass" for c in self.checks)

    @property
    def verdict(self) -> str:
        """``VALID`` (everything proven), ``INCOMPLETE`` (nothing failed, but
        something the file claims is not shown) or ``INVALID``."""
        return "VALID" if self.proven else "INCOMPLETE" if self.ok else "INVALID"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ok": self.ok,
            "proven": self.proven,
            "bundle_kind_ok": self.kind_ok,
            "bundle_digest": self.bundle_digest,
            "root": self.root,
            "checks": [
                {"name": c.name, "status": c.status, "findings": list(c.findings), "plain": c.plain}
                for c in self.checks
            ],
            "records": [
                {
                    "capsule_id": r.capsule_id,
                    "identity_ok": r.identity_ok,
                    "signature": r.signature,
                    "key_id": r.key_id,
                    "seq": r.seq,
                    "messages": list(r.messages),
                }
                for r in self.records
            ],
            "checkpoint_authenticated": self.checkpoint_authenticated,
            "signer_matches_checkpoint": self.signer_matches_checkpoint,
            "missing": list(self.missing),
            "closure_depth": self.closure_depth,
            "countersignatures_unverified": self.countersignatures,
            "extensions_uninterpreted": list(self.extensions),
            "witnesses": None
            if self.witness is None
            else {
                "status": self.witness.status,
                "findings": list(self.witness.findings),
                "plain": self.witness.plain,
                "receipts": list(self.witness_receipts),
            },
        }


def load_evidence_file(path: str | Path) -> Any:
    """Parse ``path`` as JSON. Raises ``ValueError`` for a file that is not."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"{path}: not a readable JSON file ({err})") from err


def check_evidence_file(
    bundle: Any, *, require_signature: bool = False, witness_directory: Any = None
) -> EvidenceFileCheck:
    """Check ``bundle`` offline. Never raises; a malformed file is a failed
    check, not an exception. ``witness_directory`` is a parsed
    ``witnesses.json`` naming the witnesses and keys to accept; ``None``
    accepts none, so every receipt is ``withheld``."""
    from agent_action_capsule.bundle import verify_bundle

    if not isinstance(bundle, dict):
        return EvidenceFileCheck(
            kind_ok=False,
            bundle_digest=None,
            root=None,
            checks=[Check("records", "fail", ("not_an_evidence_bundle_v2",))],
        )
    result = verify_bundle(
        bundle, strict=True, require_signature=require_signature, witness_directory=witness_directory
    )
    checks = [Check(name, claim.status, tuple(claim.findings)) for name, claim in result.claims]
    if not result.kind_ok:
        return EvidenceFileCheck(kind_ok=False, bundle_digest=None, root=None, checks=checks)

    witness = Check("witnesses", result.witnesses.status, tuple(result.witnesses.findings))
    completeness = bundle.get("completeness") if isinstance(bundle.get("completeness"), dict) else {}
    missing = completeness.get("missing") if isinstance(completeness.get("missing"), list) else []
    depth = completeness.get("closure_depth", 2)
    extensions = bundle.get("extensions") if isinstance(bundle.get("extensions"), dict) else {}
    return EvidenceFileCheck(
        kind_ok=True,
        bundle_digest=result.bundle_digest,
        root=bundle.get("root") if isinstance(bundle.get("root"), str) else None,
        checks=checks,
        records=[
            RecordCheck(r.capsule_id, r.identity_ok, r.signature, r.key_id, r.seq, r.messages)
            for r in result.records
        ],
        checkpoint=dict(result.signed_checkpoint),
        checkpoint_authenticated=bool(result.signed_checkpoint),
        signer_matches_checkpoint=result.signer_matches_checkpoint,
        missing=tuple(str(m) for m in missing),
        closure_depth=depth if isinstance(depth, int) and not isinstance(depth, bool) else None,
        countersignatures=len(result.countersignatures),
        extensions=tuple(sorted(str(k) for k in extensions)),
        witness=witness,
        witness_receipts=[
            {
                "ts_url": w.ts_url,
                "binding": w.binding,
                "status": w.status,
                "reason": w.reason,
                "registration_time": w.registration_time,
                "registration_time_reason": w.registration_time_reason,
            }
            for w in result.witness_receipts
        ],
    )
