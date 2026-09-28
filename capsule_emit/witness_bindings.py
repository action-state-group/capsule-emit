# SPDX-License-Identifier: Apache-2.0
"""Witness bindings -- how one checkpoint reaches more than one kind of
transparency service -- and the plurality policy a verifier applies to the
receipts that come back.

"More than one witness" is a COUNT of receipts a checkpoint carries, checked
against a policy the verifier chooses. It is never a list of trusted services
this library ships, and no service is privileged here: the policy counts
receipts that verify offline, whoever issued them.

**Choosing a binding.** A witness endpoint's binding is named by its URL
scheme, so the existing ``witness_url=`` / ``CAPSULE_WITNESS_URL`` value
(one URL, a list, or a comma-separated string) carries it with no new
configuration surface:

- ``https://host`` (or ``http://``) -- ``cll``: the checkpointed-local-log
  witness route, ``POST /checkpoints`` (the existing default).
- ``rekor+https://rekor.sigstore.dev`` -- ``rekor``: a Sigstore Rekor log.
- ``scrapi+https://host`` -- ``scrapi``: a SCITT Transparency Service's SCRAPI
  ``POST /entries`` route (draft-ietf-scitt-scrapi).

The full scheme-prefixed URL is what is recorded on the ``WitnessRecord``
(``ts_url``), so the durable per-witness backlog and backfill machinery in
``capsule_emit.witness`` keys each binding separately with no schema change.

**Rekor: ``dsse``, not ``hashedrekord``.** Rekor verifies an Ed25519
``hashedrekord`` signature as Ed25519ph over a SHA-512 digest
(``rekor/pkg/types/hashedrekord/v0.0.1/entry.go``: ``WithED25519ph()``;
``sigstore/pkg/signature/ed25519ph.go``: SHA-512 only). A checkpoint key signs
plain Ed25519, so a ``hashedrekord`` entry from it is rejected. The ``dsse``
type carries the envelope, so Rekor checks a plain Ed25519 signature over
the DSSE pre-authentication encoding. Rekor stores only the payload and
envelope hashes, not the payload. The payload is the checkpoint's COSE
statement: log shape only (size, root, time, key id), never capsule content.

**What each receipt proves (grade).** A Rekor entry proves the checkpoint
bytes existed, signed by the log's key, at Rekor's integrated time:
``countersigned-observed``. Rekor never checks MMR consistency, so a Rekor
receipt is never ``mmr-verified``. A SCRAPI receipt is graded by its own
protected-header label when it has one; a receipt that verifies but carries
no label proves inclusion (existence and time) and is
``countersigned-observed``. A ``cll`` receipt keeps its own label, read by
``capsule_emit.witness._receipt_grade``.

Every verification here is offline and takes its keys from one place: the
witness directory (``witnesses.json``, see ``capsule_emit.witness_directory``).
Every row is read the same way, and no service -- including the library's
own default witness, or public Rekor -- has a key built in here. A receipt
from a witness with no directory row is ``not checked``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BINDING_CLL",
    "BINDING_REKOR",
    "BINDING_SCRAPI",
    "BINDINGS",
    "PUBLIC_REKOR_URL",
    "WitnessBindingError",
    "binding_of",
    "endpoint_of",
    "operator_of",
    "dsse_pae",
    "build_rekor_dsse_entry",
    "register_rekor",
    "register_scrapi",
    "verify_rekor_receipt",
    "verify_scrapi_receipt",
    "WitnessPolicy",
    "ReceiptVerdict",
    "PluralityResult",
    "verify_witnesses",
]

BINDING_CLL = "cll"
BINDING_REKOR = "rekor"
BINDING_SCRAPI = "scrapi"
BINDINGS = (BINDING_CLL, BINDING_REKOR, BINDING_SCRAPI)

#: The public-good Rekor instance, as a witness URL. A URL only: its key is
#: read from the witness directory like every other witness's.
PUBLIC_REKOR_URL = "rekor+https://rekor.sigstore.dev"

_REKOR_ENTRIES_PATH = "/api/v1/log/entries"
_SCRAPI_ENTRIES_PATH = "/entries"
_COUNTERSIGNED_OBSERVED = "countersigned-observed"
_RECEIPT_GRADES = frozenset({_COUNTERSIGNED_OBSERVED, "mmr-verified"})


class WitnessBindingError(RuntimeError):
    """A binding could not register a checkpoint (unreachable, refused, or
    an unusable response). Callers on the checkpoint path catch it per
    witness: one binding failing never blocks another."""


# -- binding selection --------------------------------------------------------


def binding_of(url: str) -> str:
    """The binding a witness URL names: ``rekor+...`` -> ``rekor``,
    ``scrapi+...`` -> ``scrapi``, anything else -> ``cll``."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme.startswith(BINDING_REKOR + "+"):
        return BINDING_REKOR
    if scheme.startswith(BINDING_SCRAPI + "+"):
        return BINDING_SCRAPI
    return BINDING_CLL


def endpoint_of(url: str) -> str:
    """The HTTP(S) base URL to dial, with any binding prefix removed and no
    trailing slash."""
    if binding_of(url) != BINDING_CLL:
        url = url.split("+", 1)[1]
    return url.rstrip("/")


def operator_of(url: str, directory: Any = None) -> str:
    """Who operates the witness at ``url``, for the distinct-operators rule:
    the ``name`` of its row in ``directory`` (a parsed ``witnesses.json``,
    or its ``witnesses`` list). Without a row, the endpoint's host name --
    two endpoints on the same host count as one operator, the conservative
    reading."""
    from .witness_directory import row_for

    row = row_for(directory, url)
    if row is not None:
        return row["name"]
    return (urllib.parse.urlsplit(endpoint_of(url)).hostname or url).lower()


# -- Rekor (dsse) -------------------------------------------------------------


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding -- the exact bytes a DSSE
    signature covers."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def _ed25519_spki_pem(raw_pubkey: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PublicKey.from_public_bytes(raw_pubkey)
    return key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def build_rekor_dsse_entry(
    checkpoint_cose: bytes, payload_type: str, signature: bytes, raw_pubkey: bytes
) -> dict:
    """The body Rekor expects on ``POST /api/v1/log/entries`` for a ``dsse``
    v0.0.1 entry: the envelope (as a JSON string) plus the verifier key."""
    envelope = {
        "payloadType": payload_type,
        "payload": base64.b64encode(checkpoint_cose).decode("ascii"),
        "signatures": [{"keyid": "", "sig": base64.b64encode(signature).decode("ascii")}],
    }
    return {
        "apiVersion": "0.0.1",
        "kind": "dsse",
        "spec": {
            "proposedContent": {
                "envelope": json.dumps(envelope, separators=(",", ":")),
                "verifiers": [base64.b64encode(_ed25519_spki_pem(raw_pubkey)).decode("ascii")],
            }
        },
    }


def _http(req: urllib.request.Request, timeout: float) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def register_rekor(
    checkpoint_cose: bytes,
    url: str,
    *,
    entry_hash: str,
    sign: Any,
    timeout: float = 30.0,
) -> Any:
    """Log ``checkpoint_cose`` in the Rekor instance at ``url`` as a ``dsse``
    entry signed by the checkpoint's own key, and return a ``WitnessRecord``.

    ``sign`` is ``capsule_emit.signing.Signer.sign`` (or anything with its
    shape): ``sign(bytes) -> (signature_hex, key_id_hex)``, a plain Ed25519
    signature and the raw public key it verifies under.

    ``entry_hash`` is recorded unchanged -- the same
    ``sha256(checkpoint digest)`` a ``cll`` receipt carries -- so every
    binding's record for one checkpoint names that checkpoint the same way.
    The receipt (``receipt_b64``) is the Rekor response entry itself, as
    JSON: ``{uuid, body, integratedTime, logID, logIndex, verification}``.
    """
    from .checkpoint import WitnessRecord
    from .checkpoint.cose_wire import CLL_CHECKPOINT_CONTENT_TYPE

    signature_hex, key_id = sign(dsse_pae(CLL_CHECKPOINT_CONTENT_TYPE, checkpoint_cose))
    body = build_rekor_dsse_entry(
        checkpoint_cose,
        CLL_CHECKPOINT_CONTENT_TYPE,
        bytes.fromhex(signature_hex),
        bytes.fromhex(key_id),
    )
    req = urllib.request.Request(
        endpoint_of(url) + _REKOR_ENTRIES_PATH,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    status, _headers, raw = _http(req, timeout)
    if status not in (200, 201):
        raise WitnessBindingError(f"Rekor returned HTTP {status}: {raw[:300].decode(errors='replace')}")
    try:
        parsed = json.loads(raw)
        uuid, entry = next(iter(parsed.items()))
        receipt = {"uuid": uuid, **entry}
        log_index = int(entry["logIndex"])
        proof = (entry.get("verification") or {}).get("inclusionProof") or {}
        tree_size = int(proof.get("treeSize", 0))
    except (ValueError, StopIteration, KeyError, AttributeError, TypeError) as exc:
        raise WitnessBindingError(f"unusable Rekor response: {exc}") from exc
    return WitnessRecord(
        ts_url=url,
        entry_hash=entry_hash,
        receipt_b64=base64.b64encode(json.dumps(receipt, sort_keys=True).encode()).decode("ascii"),
        leaf_index=log_index,
        tree_size=tree_size,
    )


def _canonical_json(obj: Any) -> bytes:
    # Sorted keys, no whitespace. Enough for RFC 8785 here: every value
    # canonicalized below is a string or an integer.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_rekor_receipt(
    receipt: dict,
    *,
    checkpoint_cose: bytes,
    checkpoint_key_id: str,
    rekor_public_key_pem: bytes,
) -> tuple[bool, str]:
    """Offline check of one Rekor entry against the checkpoint it claims.

    Passes only if all hold:

    1. **Rekor signed it.** The Signed Entry Timestamp verifies under
       ``rekor_public_key_pem`` over ``{body, integratedTime, logID,
       logIndex}``, and ``logID`` is that key's id.
    2. **It is this checkpoint.** The entry body is a ``dsse`` entry whose
       ``payloadHash`` is ``sha256(checkpoint_cose)``.
    3. **The log's own key signed it.** The entry's only verifier key is the
       Ed25519 key ``checkpoint_key_id`` names -- so someone else logging
       our checkpoint bytes under their key does not count as our receipt.

    Returns ``(ok, reason)``; never raises."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_public_key,
        )

        rekor_key = load_pem_public_key(rekor_public_key_pem)
        log_id = hashlib.sha256(
            rekor_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        ).hexdigest()
        if receipt.get("logID") != log_id:
            return False, "logID is not the pinned Rekor key"
        set_b64 = (receipt.get("verification") or {}).get("signedEntryTimestamp")
        if not set_b64:
            return False, "no signedEntryTimestamp"
        signed = _canonical_json(
            {
                "body": receipt["body"],
                "integratedTime": int(receipt["integratedTime"]),
                "logID": receipt["logID"],
                "logIndex": int(receipt["logIndex"]),
            }
        )
        try:
            rekor_key.verify(base64.b64decode(set_b64), signed, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return False, "signedEntryTimestamp does not verify under the pinned Rekor key"

        entry = json.loads(base64.b64decode(receipt["body"]))
        if entry.get("kind") != "dsse":
            return False, f"entry kind {entry.get('kind')!r}, expected 'dsse'"
        spec = entry.get("spec") or {}
        payload_hash = (spec.get("payloadHash") or {}).get("value")
        if payload_hash != hashlib.sha256(checkpoint_cose).hexdigest():
            return False, "entry payloadHash is not this checkpoint's bytes"
        sigs = spec.get("signatures") or []
        expected_pem = _ed25519_spki_pem(bytes.fromhex(checkpoint_key_id))
        verifiers = {
            base64.b64decode(s.get("verifier", "")).replace(b"\r", b"").strip() for s in sigs
        }
        if verifiers != {expected_pem.strip()}:
            return False, "entry is not signed by the checkpoint's key"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 -- verification degrades to "no", never raises
        return False, f"unreadable receipt: {exc}"


# -- SCRAPI -------------------------------------------------------------------


def register_scrapi(
    checkpoint_cose: bytes,
    url: str,
    *,
    entry_hash: str,
    timeout: float = 30.0,
    poll_interval: float = 1.0,
    max_polls: int = 10,
) -> Any:
    """Register ``checkpoint_cose`` -- already a COSE_Sign1 signed statement --
    with the SCRAPI service at ``url`` and return a ``WitnessRecord`` whose
    ``receipt_b64`` is the service's COSE receipt.

    Follows draft-ietf-scitt-scrapi registration: ``POST /entries``
    (``application/cose``). ``201`` returns the receipt in the body. ``202``
    or ``303`` names a ``Location`` to poll; polling stops when it answers
    ``200`` with ``application/cose`` (the receipt), or redirects to the entry
    whose ``GET`` is the receipt."""
    from .checkpoint import WitnessRecord

    base = endpoint_of(url)
    req = urllib.request.Request(
        base + _SCRAPI_ENTRIES_PATH,
        data=checkpoint_cose,
        method="POST",
        headers={"Content-Type": "application/cose", "Accept": "application/cose"},
    )
    status, headers, raw = _http(req, timeout)
    polls = 0
    while status in (202, 302, 303):
        location = headers.get("Location") or headers.get("location")
        if not location or polls >= max_polls:
            raise WitnessBindingError(f"SCRAPI registration at {base} did not complete (HTTP {status})")
        polls += 1
        if status == 202:
            time.sleep(poll_interval)
        get = urllib.request.Request(
            urllib.parse.urljoin(base + "/", location), headers={"Accept": "application/cose"}
        )
        status, headers, raw = _http(get, timeout)
    if status not in (200, 201) or not raw:
        raise WitnessBindingError(f"SCRAPI returned HTTP {status}: {raw[:300].decode(errors='replace')}")
    return WitnessRecord(
        ts_url=url,
        entry_hash=entry_hash,
        receipt_b64=base64.b64encode(raw).decode("ascii"),
        leaf_index=0,
        tree_size=0,
    )


def verify_scrapi_receipt(
    receipt_bytes: bytes,
    *,
    checkpoint_cose: bytes,
    service_public_key_pem: bytes | str | None,
) -> tuple[bool, str, str | None]:
    """Offline check of a SCRAPI receipt: an RFC 9162 inclusion receipt over
    the leaf ``sha256(checkpoint_cose)``, signed by the key the verifier
    pinned for that service. Returns ``(ok, reason, grade)``.

    Without a pinned key nothing is checked (``not checked``, no grade):
    a key served by the service itself would prove nothing. A service whose
    receipts use another tree algorithm (e.g. CCF) also fails here and does
    not count until a verifier for that format is added."""
    if service_public_key_pem is None:
        return False, "not checked: no key pinned for this service", None
    try:
        from scitt_cose import verify_receipt
    except ImportError:
        return False, "not checked: scitt_cose not installed", None
    try:
        result = verify_receipt(
            receipt_bytes,
            leaf_entry_hex=hashlib.sha256(checkpoint_cose).hexdigest(),
            log_public_key_pem=service_public_key_pem,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable receipt: {exc}", None
    if not result.ok:
        return False, "receipt does not verify for this checkpoint under the pinned key", None
    label = (getattr(result, "protected_header_ext", None) or {}).get(-65537)
    if label is None:
        return True, "ok", _COUNTERSIGNED_OBSERVED
    if label in _RECEIPT_GRADES:
        return True, "ok", label
    return True, "ok (unknown grade label ignored)", _COUNTERSIGNED_OBSERVED


# -- plurality policy ---------------------------------------------------------


@dataclass(frozen=True)
class WitnessPolicy:
    """What a verifier requires of one checkpoint's receipts.

    ``min_receipts`` -- how many receipts must verify offline (k of however
    many the checkpoint carries). ``distinct_operators`` -- when true, those
    k must come from k different operators (see :func:`operator_of`)."""

    min_receipts: int = 1
    distinct_operators: bool = True


@dataclass(frozen=True)
class ReceiptVerdict:
    ts_url: str
    binding: str
    operator: str
    verified: bool
    grade: str | None
    reason: str


@dataclass(frozen=True)
class PluralityResult:
    """Per-receipt verdicts plus the policy outcome. ``counted`` is how many
    receipts verified; ``operators`` is how many distinct operators issued
    them. ``policy_met`` is the only yes/no -- it says the policy was
    satisfied, never that any service is trusted."""

    policy: WitnessPolicy
    receipts: list[ReceiptVerdict] = field(default_factory=list)
    counted: int = 0
    operators: int = 0
    policy_met: bool = False

    def summary(self) -> str:
        """e.g. ``"2 witnesses · 2 operators"`` -- counts, never names."""
        w = "witness" if self.counted == 1 else "witnesses"
        o = "operator" if self.operators == 1 else "operators"
        return f"{self.counted} {w} · {self.operators} {o}"


def _verify_one(checkpoint: Any, w: Any, pem: bytes, cose: bytes | None) -> tuple[bool, str, str | None]:
    """One receipt under one key. The same call for every row's key."""
    binding = binding_of(w.ts_url)
    if binding == BINDING_CLL:
        from .checkpoint import StampVerdict, verify_witness_stamp_tristate
        from .witness import _receipt_grade

        grade = _receipt_grade(checkpoint, w, ts_pubkey_pem=pem)
        if grade is not None:
            return True, "ok", grade
        verdict, errors = verify_witness_stamp_tristate(checkpoint, w, ts_pubkey_pem=pem)
        if verdict is StampVerdict.WITNESSED:
            return True, "ok (receipt carries no grade label)", None
        return False, "; ".join(errors) or str(verdict), None
    if cose is None:
        return False, "not checked: checkpoint COSE statement not available", None
    if binding == BINDING_REKOR:
        try:
            receipt = json.loads(base64.b64decode(w.receipt_b64))
        except ValueError as exc:
            return False, f"unreadable receipt: {exc}", None
        ok, reason = verify_rekor_receipt(
            receipt, checkpoint_cose=cose, checkpoint_key_id=checkpoint.key_id, rekor_public_key_pem=pem
        )
        return ok, reason, _COUNTERSIGNED_OBSERVED if ok else None
    return verify_scrapi_receipt(
        base64.b64decode(w.receipt_b64), checkpoint_cose=cose, service_public_key_pem=pem
    )


def verify_witnesses(
    checkpoint: Any,
    witnesses: Any,
    *,
    directory: Any,
    checkpoint_cose_hex: str | None = None,
    policy: WitnessPolicy | None = None,
) -> PluralityResult:
    """Verify every receipt ``checkpoint`` carries and apply ``policy``.

    ``witnesses`` is an iterable of ``WitnessRecord`` (or a
    ``{ts_url: WitnessRecord}`` mapping, e.g.
    ``CheckpointWitnessState.effective_witnesses``). ``directory`` is a
    parsed ``witnesses.json`` (or its ``witnesses`` list) -- the verifier's
    choice of which witnesses and keys it accepts, and the ONLY source of
    keys and operator names here. Every receipt is resolved the same way:
    its row (``witness_directory.row_for``), then each of that row's keys in
    turn (``witness_directory.row_public_keys_pem``, so a rotated key still
    verifies). No witness has a built-in key. A receipt with no row is
    ``not checked``.

    ``checkpoint_cose_hex`` is the checkpoint's persisted COSE statement,
    which ``rekor`` and ``scrapi`` receipts bind to; without it those
    receipts are ``not checked``.

    A receipt that does not verify is reported with its reason and does not
    count. Stub receipts never count. Offline; never raises."""
    from .witness_directory import row_for, row_public_keys_pem

    policy = policy or WitnessPolicy()
    records = list(witnesses.values()) if isinstance(witnesses, dict) else list(witnesses)
    cose = bytes.fromhex(checkpoint_cose_hex) if checkpoint_cose_hex else None

    verdicts: list[ReceiptVerdict] = []
    for w in records:
        binding = binding_of(w.ts_url)
        operator = operator_of(w.ts_url, directory)
        row = row_for(directory, w.ts_url)
        ok, reason, grade = False, "", None
        if getattr(w, "is_stub", False):
            reason = "stub receipt (never reached a witness)"
        elif row is None:
            reason = "not checked: no directory row for this witness"
        else:
            try:
                pems = row_public_keys_pem(row)
            except ValueError as exc:
                pems, reason = [], f"not checked: unusable key in directory row ({exc})"
            for pem in pems:
                ok, reason, grade = _verify_one(checkpoint, w, pem, cose)
                if ok:
                    break
        verdicts.append(ReceiptVerdict(w.ts_url, binding, operator, ok, grade if ok else None, reason))

    good = [v for v in verdicts if v.verified]
    operators = len({v.operator for v in good})
    enough = operators if policy.distinct_operators else len(good)
    return PluralityResult(
        policy=policy,
        receipts=verdicts,
        counted=len(good),
        operators=operators,
        policy_met=enough >= policy.min_receipts,
    )
