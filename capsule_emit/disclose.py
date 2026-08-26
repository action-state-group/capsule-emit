# SPDX-License-Identifier: Apache-2.0
"""``disclose()`` — the deliberate, recorded act of handing bundle + content
to an audience (O16 audit item 10, frozen dev-surface v4 §7b).

``disclose`` is ``bundle`` (O16 audit item 14, ``capsule_emit.bundle``) plus:
selected payload content (the existing single-capsule Disclosure Envelope,
``capsule_emit.disclosure.build_disclosure_envelope``, applied per record), a
**completeness statement** so a partial disclosure can never read as a full
one (the equivocation-honesty rule), an **audience-suppression profile**
(fields the caller explicitly withholds for this audience, recorded rather
than silently dropped), and its own **self-sealing disclosure record** —
signed with the same producer ``Signer`` ``seal()`` uses
(``capsule_emit.signing``), and appended to the SAME ledger as a new
``kind`` (``capsule_emit.ledger.DISCLOSURE_RECORD_KIND``) — so it becomes an
MMR leaf like any other entry: the act of showing evidence is itself
evidence (frozen surface §7b: "disclosures are receipts too").

``bundle`` stays the always-safe verb (digests only); this module is its
conscious sibling — reached only when content is about to cross a boundary
to another party. Reading your own ledger (``ledger show``, ``status``)
mints no receipt; only a call into this module does.

**Record selection** — the positional ``<id|range>``, two neutral forms
(``verify_disclosure``/CLI ``disclose`` parse the same syntax):

- a single ``capsule_id`` (full, or an unambiguous >=8-char prefix) — always
  ``"contiguous"`` completeness (a range of one).
- ``id1..id2`` — a CONTIGUOUS range, inclusive, in ledger order — the honest
  ``"contiguous"`` completeness mode: nothing between the endpoints is
  omitted.
- ``id1,id2,...`` — an explicit, arbitrary list — always ``"producer-selected"``
  completeness, regardless of whether the ids happen to be contiguous,
  because the caller chose to enumerate rather than bound a range.

Both are brownfield-runnable, neutral primitives (frozen surface §7c: "a
brownfield user without our compiler can produce every disclosure our
compiler can"). ``--claim``-driven selection (resolving a rollup's evidence
rule to exactly the records it cites) is plugin sugar layered OVER this
primitive, not part of it — out of scope here by the same boundary test.

**Payload completeness** (``payloads="all"|"selected"``): ``"all"`` requires
a supplied payload for every ``capsule_emit.disclosure.DISCLOSURE_ELIGIBLE_FIELDS``
member that has a committed digest on every selected record, UNLESS that
field is explicitly named in ``suppress`` — :func:`disclose` refuses
(raises :class:`DiscloseError`) rather than silently ship a disclosure that
claims completeness while quietly withholding a field. ``"selected"``
discloses exactly the payloads supplied, nothing implied either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Disclosure", "DiscloseError", "disclose", "verify_disclosure"]

_PAYLOAD_MODES = ("all", "selected")


class DiscloseError(RuntimeError):
    """A disclosure cannot be honestly built as requested — bad selector,
    an unbundle-able record, a payload/suppress contradiction, or an
    incomplete ``payloads="all"`` request."""


@dataclass(frozen=True)
class Disclosure:
    """A standalone, JSON-round-trippable disclosure — one or more
    :class:`~capsule_emit.bundle.Bundle` s, the payload content actually
    disclosed for each, the completeness statement, and the sealed
    disclosure record itself."""

    v: int
    audience: str
    record_ids: tuple[str, ...]  # capsule_ids disclosed, in ledger order
    bundles: dict[str, Any]  # capsule_id -> capsule_emit.bundle.Bundle
    envelopes: dict[str, dict]  # capsule_id -> {"capsule": ..., "disclosures": {...}}
    completeness: dict[str, Any]
    suppressed_fields: tuple[str, ...]
    disclosure_record: dict  # the sealed, persisted ledger entry

    def to_dict(self) -> dict:
        return {
            "v": self.v,
            "audience": self.audience,
            "record_ids": list(self.record_ids),
            "bundles": {cid: b.to_dict() for cid, b in self.bundles.items()},
            "envelopes": self.envelopes,
            "completeness": self.completeness,
            "suppressed_fields": list(self.suppressed_fields),
            "disclosure_record": self.disclosure_record,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Disclosure:
        from .bundle import Bundle

        return cls(
            v=int(d["v"]),
            audience=d["audience"],
            record_ids=tuple(d["record_ids"]),
            bundles={cid: Bundle.from_dict(b) for cid, b in d["bundles"].items()},
            envelopes=d["envelopes"],
            completeness=d["completeness"],
            suppressed_fields=tuple(d.get("suppressed_fields", ())),
            disclosure_record=d["disclosure_record"],
        )


def _match_one(records: list[dict], token: str) -> tuple[int, dict]:
    token = token.strip()
    matches = [
        (i, r)
        for i, r in enumerate(records)
        if r.get("capsule_id") == token
        or (len(token) >= 8 and str(r.get("capsule_id", "")).startswith(token))
    ]
    if not matches:
        raise DiscloseError(f"no record matches capsule_id {token!r}")
    exact = [(i, r) for i, r in matches if r["capsule_id"] == token]
    if exact:
        return exact[0]
    if len(matches) > 1:
        raise DiscloseError(
            f"capsule_id prefix {token!r} matches {len(matches)} records — use more characters"
        )
    return matches[0]


def _resolve_selector(records: list[dict], selector: str) -> tuple[list[dict], str]:
    """Resolve the ``<id|range>`` positional to ``(ordered records, records_mode)``.

    ``records_mode`` is ``"contiguous"`` for a single id or an ``id1..id2``
    range, ``"producer-selected"`` for an explicit ``id1,id2,...`` list —
    see the module docstring.
    """
    if ".." in selector:
        left, _, right = selector.partition("..")
        i0, _ = _match_one(records, left)
        i1, _ = _match_one(records, right)
        if i0 > i1:
            raise DiscloseError(f"range {selector!r} is backwards in ledger order — swap the endpoints")
        return records[i0 : i1 + 1], "contiguous"

    if "," in selector:
        tokens = [t for t in selector.split(",") if t.strip()]
        picked: list[dict] = []
        seen: set[str] = set()
        for t in tokens:
            _, rec = _match_one(records, t)
            cid = rec["capsule_id"]
            if cid not in seen:
                seen.add(cid)
                picked.append(rec)
        order = {r["capsule_id"]: i for i, r in enumerate(records)}
        picked.sort(key=lambda r: order[r["capsule_id"]])
        return picked, "producer-selected"

    _, rec = _match_one(records, selector)
    return [rec], "contiguous"


def _missing_for_all(
    records: list[dict], reveal: dict[str, dict[str, Any]], suppress: frozenset[str]
) -> list[tuple[str, str]]:
    from .disclosure import DISCLOSURE_ELIGIBLE_FIELDS

    missing = []
    for r in records:
        cid = r["capsule_id"]
        ca = (r.get("model_attestation") or {}).get("compute_attestation") or {}
        supplied = reveal.get(cid, {})
        for field, digest_field in DISCLOSURE_ELIGIBLE_FIELDS.items():
            if field in suppress:
                continue
            if ca.get(digest_field) and field not in supplied:
                missing.append((cid, field))
    return missing


def _completeness(
    records: list[dict], records_mode: str, payloads_mode: str, suppress: tuple[str, ...]
) -> dict:
    n = len(records)
    if records_mode == "contiguous":
        if n == 1:
            records_note = f"1 record ({records[0]['capsule_id'][:12]}…) — contiguous, nothing omitted"
        else:
            first, last = records[0]["capsule_id"], records[-1]["capsule_id"]
            records_note = f"{n} of {n} record(s), {first[:12]}…..{last[:12]}… — contiguous, nothing omitted"
    else:
        records_note = f"{n} producer-selected record(s), explicit list — not a bound range"

    payloads_note = (
        "every eligible payload field disclosed for every selected record"
        if payloads_mode == "all"
        else "producer-selected payload field(s) only"
    )
    if suppress:
        payloads_note += (
            f"; {len(suppress)} field(s) explicitly suppressed for this audience: {', '.join(suppress)}"
        )

    return {
        "records_mode": records_mode,
        "records_note": records_note,
        "payloads_mode": payloads_mode,
        "payloads_note": payloads_note,
    }


def disclose(
    path: Any,
    selector: str,
    *,
    audience: str,
    payloads: str = "all",
    reveal: dict[str, dict[str, Any]] | None = None,
    suppress: list[str] | tuple[str, ...] | None = None,
    signer: Any = None,
    signing_key_path: Any = None,
) -> Disclosure:
    """Build and seal a :class:`Disclosure` for ``selector`` against the
    JSONL ledger at ``path``: bundle + selected payload content +
    completeness statement + audience-suppression profile, and append its
    own sealed disclosure record to the SAME ledger.

    ``reveal`` maps ``capsule_id -> {field: value}`` (``field`` one of
    ``capsule_emit.disclosure.DISCLOSURE_ELIGIBLE_FIELDS``) — the payload
    bytes actually being handed to ``audience`` for that record; a record
    with no entry (or an empty one) discloses no payload content, bundle
    only. Every key must belong to a record the selector resolved to.

    ``suppress`` names fields withheld from EVERY selected record for this
    audience, regardless of whether ``reveal`` supplied them — supplying a
    payload for a suppressed field is a contradiction and raises
    :class:`DiscloseError`.

    Raises :class:`DiscloseError` if: ``payloads`` is not ``"all"``/``"selected"``;
    the selector doesn't resolve; ``reveal``/``suppress`` name an unknown
    field, an unselected record, or contradict each other; any selected
    record isn't yet bundle-able (``capsule_emit.bundle.BundleError`` is
    wrapped, not swallowed); a disclosed payload doesn't match its committed
    digest (``capsule_emit.disclosure.DisclosureError`` wrapped likewise);
    or ``payloads="all"`` is requested but a non-suppressed eligible field
    is missing a payload.
    """
    import os

    from .bundle import BundleError
    from .bundle import bundle as _bundle_fn
    from .disclosure import DISCLOSURE_ELIGIBLE_FIELDS, DisclosureError
    from .disclosure import build_disclosure_envelope as _build_envelope
    from .ledger import DISCLOSURE_RECORD_KIND, append_to_ledger, read_ledger

    if payloads not in _PAYLOAD_MODES:
        raise DiscloseError(f"payloads must be one of {_PAYLOAD_MODES!r}, got {payloads!r}")

    records = read_ledger(path)
    if not records:
        raise DiscloseError(f"{path}: empty or not found")

    selected, records_mode = _resolve_selector(records, selector)
    selected_ids = {r["capsule_id"] for r in selected}

    reveal = reveal or {}
    for cid in reveal:
        if cid not in selected_ids:
            raise DiscloseError(
                f"reveal supplied for capsule_id {cid!r}, which is not part of the {selector!r} selection"
            )
        for field in reveal[cid]:
            if field not in DISCLOSURE_ELIGIBLE_FIELDS:
                raise DiscloseError(
                    f"reveal: {cid} names field {field!r}, not one of {sorted(DISCLOSURE_ELIGIBLE_FIELDS)}"
                )

    suppress_set = frozenset(suppress or ())
    unknown_suppress = suppress_set - DISCLOSURE_ELIGIBLE_FIELDS.keys()
    if unknown_suppress:
        raise DiscloseError(
            f"suppress names unknown field(s) {sorted(unknown_suppress)}, not a subset of "
            f"{sorted(DISCLOSURE_ELIGIBLE_FIELDS)}"
        )
    for cid, fields in reveal.items():
        conflict = suppress_set & fields.keys()
        if conflict:
            raise DiscloseError(
                f"{cid}: reveal supplies {sorted(conflict)} but the same field(s) are in "
                "suppress — a field cannot be disclosed and suppressed in the same call"
            )

    if payloads == "all":
        missing = _missing_for_all(selected, reveal, suppress_set)
        if missing:
            detail = ", ".join(f"{cid[:12]}…:{field}" for cid, field in missing)
            raise DiscloseError(
                f"payloads='all' but {len(missing)} eligible field(s) have no supplied "
                f"payload and are not suppressed: {detail} — supply a payload, add to "
                "suppress, or pass payloads='selected'"
            )

    envelopes: dict[str, dict] = {}
    bundles: dict[str, Any] = {}
    for r in selected:
        cid = r["capsule_id"]
        try:
            bundles[cid] = _bundle_fn(path, cid)
        except BundleError as exc:
            raise DiscloseError(f"{cid}: cannot bundle — {exc}") from exc

        fields = dict(reveal.get(cid, {}))
        for suppressed_field in suppress_set:
            fields.pop(suppressed_field, None)
        try:
            envelopes[cid] = _build_envelope(
                r,
                agent_input=fields.get("agent_input"),
                agent_output=fields.get("agent_output"),
                strict=True,
            )
        except DisclosureError as exc:
            raise DiscloseError(f"{cid}: {exc}") from exc

    completeness = _completeness(selected, records_mode, payloads, tuple(sorted(suppress_set)))

    record = {
        "kind": DISCLOSURE_RECORD_KIND,
        "v": 1,
        "audience": audience,
        "selector": selector,
        "disclosed_capsule_ids": [r["capsule_id"] for r in selected],
        "disclosed_fields": {cid: sorted(envelopes[cid]["disclosures"].keys()) for cid in envelopes},
        "suppressed_fields": sorted(suppress_set),
        "completeness": completeness,
        "timestamp": _now_iso(),
    }
    record = _seal_record(record, os.fspath(path), signer=signer, signing_key_path=signing_key_path)
    append_to_ledger(record, path)

    return Disclosure(
        v=1,
        audience=audience,
        record_ids=tuple(r["capsule_id"] for r in selected),
        bundles=bundles,
        envelopes=envelopes,
        completeness=completeness,
        suppressed_fields=tuple(sorted(suppress_set)),
        disclosure_record=record,
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _seal_record(record: dict, ledger_path: str, *, signer: Any = None, signing_key_path: Any = None) -> dict:
    """Sign ``record`` with the producer's own :class:`~capsule_emit.signing.Signer`
    (the same one ``seal()`` uses for this ledger) — the identical sequencing
    ``capsule_emit.core._emit_capsule`` uses for capsules, which is what lets
    ``capsule_emit.signing.verify_capsule_signature`` verify a disclosure
    record unmodified: it operates on any dict with
    ``signature``/``key_id``/``capsule_id``, not just capsules. ``capsule_id``
    is a pure content address (excludes only itself plus ``signature``/
    ``key_id`` — see ``capsule_emit.canonicalization``), so it is computed
    and set once, then signed; no fold-in, no recompute after signing."""
    from . import signing as _signing
    from .canonicalization import compute_capsule_id

    record["capsule_id"] = compute_capsule_id(record)
    signer_obj = _signing.resolve_signer(ledger_path, signer=signer, key_path=signing_key_path)
    record["signature"], record["key_id"] = _signing.sign_producer_envelope(
        signer_obj, record["capsule_id"]
    )
    return record


def verify_disclosure(
    d: Disclosure, *, trust_anchor: dict[str, bytes | str] | None = None
) -> tuple[bool, list[str]]:
    """Pure, offline, total verification of a standalone :class:`Disclosure`
    — no reader, no network, never raises. ``trust_anchor``
    [verify-threestate-trustanchor] is forwarded unchanged to every
    ``capsule_emit.bundle.verify_bundle`` call below — see that function's
    docstring for its three-state witness-stamp semantics.

    Checks: the disclosure record's own signature
    (``capsule_emit.signing.verify_capsule_signature``); that the record's
    ``audience``/``disclosed_capsule_ids`` agree with the ``Disclosure``
    object; every bundle (``capsule_emit.bundle.verify_bundle``); and, for
    every disclosed payload field, that it recomputes to the digest
    committed on that record's receipt — a tampered payload names itself
    (frozen surface §7b) exactly the way a tampered bundle does.

    Returns ``(ok, errors)`` — ``errors`` is empty iff ``ok``.
    """
    errors: list[str] = []
    try:
        from agent_action_capsule.canonical import FloatInDigestError, json_digest

        from .bundle import verify_bundle
        from .disclosure import DISCLOSURE_ELIGIBLE_FIELDS
        from .signing import verify_capsule_signature

        if not verify_capsule_signature(d.disclosure_record):
            errors.append("disclosure record signature does not verify")

        if d.disclosure_record.get("audience") != d.audience:
            errors.append("disclosure_record.audience does not match Disclosure.audience")
        if list(d.disclosure_record.get("disclosed_capsule_ids", ())) != list(d.record_ids):
            errors.append("disclosure_record.disclosed_capsule_ids does not match Disclosure.record_ids")
        if d.completeness != d.disclosure_record.get("completeness"):
            errors.append("Disclosure.completeness does not match the signed disclosure_record.completeness")
        if list(d.suppressed_fields) != d.disclosure_record.get("suppressed_fields"):
            errors.append(
                "Disclosure.suppressed_fields does not match the signed disclosure_record.suppressed_fields"
            )

        for cid in d.record_ids:
            b = d.bundles.get(cid)
            if b is None:
                errors.append(f"{cid}: missing bundle")
                continue
            ok, berrors = verify_bundle(b, trust_anchor=trust_anchor)
            if not ok:
                errors.extend(f"{cid}: {e}" for e in berrors)

            envelope = d.envelopes.get(cid) or {}
            disclosures = envelope.get("disclosures") or {}
            ca = (b.receipt.get("model_attestation") or {}).get("compute_attestation") or {}
            for field, value in disclosures.items():
                digest_field = DISCLOSURE_ELIGIBLE_FIELDS.get(field)
                stored = ca.get(digest_field) if digest_field else None
                if stored is None:
                    errors.append(f"{cid}: {field} disclosed but no committed digest exists")
                    continue
                try:
                    computed = json_digest(value)
                except (FloatInDigestError, TypeError, ValueError) as exc:
                    errors.append(f"{cid}: {field} cannot be canonicalized: {exc}")
                    continue
                if computed != stored:
                    errors.append(f"{cid}: {field} payload does not match its committed digest")
    except Exception as exc:  # noqa: BLE001 — pure verifier, never raises
        errors.append(f"unexpected error: {exc}")
        return False, errors

    return not errors, errors
