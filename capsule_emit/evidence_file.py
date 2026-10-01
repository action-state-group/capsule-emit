# SPDX-License-Identifier: Apache-2.0
"""Check an evidence file: an AAC Evidence Bundle (``evidence-bundle/v2``,
draft-mih-zhang-agent-disclosure-bundle-00) saved by any producer.

Offline. Composes two checks that neither does alone:

* ``agent_action_capsule.bundle.verify_bundle``: every record's
  ``capsule_id`` recomputes, citation closure as declared, interval coverage
  under the checkpoint's range root, each record's inclusion proof, and the
  portable COSE checkpoint (``checkpoint.cose``) when the file carries one.
* the producer signature on every record
  (:func:`capsule_emit.signing.verify_capsule_signature_tristate`), which the
  neutral verifier deliberately leaves to the substrate.

It also states, without gating on it, whether every record names the same
signing key as the checkpoint: the log's own key vouching for records that
the log's owner signed. A key says who holds it, not who that is.

Public API
----------
check_evidence_file(bundle, *, require_signature=False) -> EvidenceFileCheck
load_evidence_file(path) -> dict
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .signing import AuthorshipVerdict, verify_capsule_signature_tristate

__all__ = [
    "BUNDLE_KIND",
    "Check",
    "EvidenceFileCheck",
    "RecordCheck",
    "check_evidence_file",
    "load_evidence_file",
]

BUNDLE_KIND = "evidence-bundle/v2"

# One plain sentence per claim status; the CLI and the report both read these.
_PLAIN: dict[str, dict[str, str]] = {
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
        "withheld": "The file carries no proof that its records are one unbroken stretch of a log.",
        "fail": "The proof that these records are one unbroken stretch of the log does not check.",
    },
    "per_record_membership": {
        "pass": "Each record is proven to be in the log at the position it claims.",
        "withheld": "The file carries no per-record proof of being in a log.",
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
    checkpoint_authenticated: bool = False
    signer_matches_checkpoint: bool | None = None
    missing: tuple[str, ...] = ()
    closure_depth: int | None = None
    countersignatures: int = 0
    extensions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """No check failed. ``withheld`` is not a failure: the file says what
        it does not prove, and the report says it too."""
        return self.kind_ok and all(c.status != "fail" for c in self.checks)

    @property
    def proven(self) -> bool:
        """Every check passed outright, nothing withheld."""
        return self.ok and all(c.status == "pass" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
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
        }


def load_evidence_file(path: str | Path) -> Any:
    """Parse ``path`` as JSON. Raises ``ValueError`` for a file that is not."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"{path}: not a readable JSON file ({err})") from err


def check_evidence_file(bundle: Any, *, require_signature: bool = False) -> EvidenceFileCheck:
    """Check ``bundle`` offline. Never raises; a malformed file is a failed
    check, not an exception."""
    from agent_action_capsule.bundle import verify_bundle

    if not isinstance(bundle, dict) or bundle.get("bundle_kind") != BUNDLE_KIND or bundle.get("bundle_version") != "2":
        return EvidenceFileCheck(
            kind_ok=False,
            bundle_digest=None,
            root=None,
            checks=[Check("records", "fail", ("not_an_evidence_bundle_v2",))],
        )

    result = verify_bundle(bundle)
    raw_records = [r for r in bundle.get("records") or [] if isinstance(r, dict)]
    memberships = _memberships(bundle)
    records: list[RecordCheck] = []
    for record in raw_records:
        cid = str(record.get("capsule_id", ""))
        verdict, messages = verify_capsule_signature_tristate(record)
        aac = result.capsule_results.get(cid)
        records.append(
            RecordCheck(
                capsule_id=cid,
                identity_ok=bool(aac is not None and aac.ok and aac.capsule_id == cid),
                signature=verdict.value,
                key_id=record.get("key_id") if isinstance(record.get("key_id"), str) else None,
                seq=memberships.get(cid),
                messages=tuple(messages),
            )
        )

    identity = Check(
        "records",
        "pass" if records and all(r.identity_ok for r in records) else "fail",
        tuple(f"record_identity_invalid:{r.capsule_id}" for r in records if not r.identity_ok)
        or (() if records else ("no_records",)),
    )
    signatures = _signature_check(records, require_signature)
    checks = [
        identity,
        signatures,
        _claim("graph_closure", result.graph_closure),
        _claim("interval_coverage", result.interval_coverage),
        _claim("per_record_membership", result.per_record_membership),
    ]

    checkpoint = bundle.get("checkpoint") if isinstance(bundle.get("checkpoint"), dict) else {}
    authenticated = (
        result.interval_coverage.status == "pass"
        and "checkpoint_unverified" not in result.interval_coverage.findings
    )
    checkpoint_key = checkpoint.get("key_id") if isinstance(checkpoint.get("key_id"), str) else None
    signer_match = (
        all(r.key_id == checkpoint_key for r in records) if checkpoint_key and records else None
    )
    completeness = bundle.get("completeness") if isinstance(bundle.get("completeness"), dict) else {}
    missing = completeness.get("missing") if isinstance(completeness.get("missing"), list) else []
    depth = completeness.get("closure_depth", 2)
    extensions = bundle.get("extensions") if isinstance(bundle.get("extensions"), dict) else {}
    return EvidenceFileCheck(
        kind_ok=True,
        bundle_digest=result.bundle_digest,
        root=bundle.get("root") if isinstance(bundle.get("root"), str) else None,
        checks=checks,
        records=records,
        checkpoint=checkpoint,
        checkpoint_authenticated=authenticated,
        signer_matches_checkpoint=signer_match,
        missing=tuple(str(m) for m in missing),
        closure_depth=depth if isinstance(depth, int) and not isinstance(depth, bool) else None,
        countersignatures=len(result.countersignatures),
        extensions=tuple(sorted(str(k) for k in extensions)),
    )


def _claim(name: str, claim: Any) -> Check:
    return Check(name, claim.status, tuple(claim.findings))


def _signature_check(records: list[RecordCheck], require_signature: bool) -> Check:
    invalid = [r for r in records if r.signature == AuthorshipVerdict.INVALID.value]
    unclaimed = [r for r in records if r.signature == AuthorshipVerdict.UNCLAIMED.value]
    findings = tuple(
        [f"producer_signature_invalid:{r.capsule_id}" for r in invalid]
        + [f"producer_signature_unclaimed:{r.capsule_id}" for r in unclaimed]
    )
    if invalid or (unclaimed and require_signature) or not records:
        return Check("signatures", "fail", findings)
    return Check("signatures", "withheld" if unclaimed else "pass", findings)


def _memberships(bundle: dict) -> dict[str, int]:
    certificate = bundle.get("completeness_certificate")
    members = certificate.get("memberships") if isinstance(certificate, dict) else None
    out: dict[str, int] = {}
    if isinstance(members, dict):
        for cid, member in members.items():
            coords = member.get("log_coordinates") if isinstance(member, dict) else None
            seq = coords.get("seq") if isinstance(coords, dict) else None
            if isinstance(seq, int) and not isinstance(seq, bool):
                out[str(cid)] = seq
    return out
