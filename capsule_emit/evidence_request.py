# SPDX-License-Identifier: Apache-2.0
"""``answer()`` — the one evidence-request responder (E14).

Cites the *shape* of the local, pre-consent evidence-request draft — a
request map ``{subject, coverage, derivation?, deadline?, nonce, profile?}``
— never the draft itself. This module owns the responder's decision logic only; it
never invents a new artifact format: a "record" subject dispatches to
:func:`capsule_emit.bundle.bundle`, a "range" subject resolves the same
``id1..id2`` / ``id1,id2,...`` selector syntax :mod:`capsule_emit.disclose`
already parses and returns one :class:`~capsule_emit.bundle.Bundle` per
selected record — **digests only**, never :func:`capsule_emit.disclose
.disclose`'s field-content reveal. That is deliberate: a ``bundle``-shaped
answer to a stranger stays digests-only regardless of what a *local*
disclosure store (PR #79's default-on text-disclosure preimage store) holds
for this node's own use — field-level disclosure to a stranger is a
separate, off-by-default decision this module never makes on its own. A
``chain_segment`` subject (``{kind: "chain_segment", from_size, to_size}``
or ``{kind: "chain_segment", last: N}``) dispatches to
:func:`capsule_emit.chain_segment.chain_segment` instead — the CHEAP form of
history: the checkpoint chain itself (signed checkpoints, witness receipts,
one consistency proof per link, per-checkpoint leaf counts by kind), never
per-record bundles. See that module's docstring for the full shape.

Every well-formed request gets exactly one of three answers — never a bare,
unsigned absence:

  * an :class:`Artifact` (one or more offline-verifiable ``Bundle`` s);
  * a :class:`Refusal` with ``reason`` naming *why* the answer withheld
    exists (``coverage_unsatisfiable``, ``request_malformed``) — a policy
    decline;
  * a :class:`Refusal` with ``reason="no_such_record"`` — the "recorded
    absence" case: this node genuinely has no such record, not a decline.

Both refusal shapes are the SAME signed object — ``{request_digest, reason,
issued_at, key_id, sig}`` — because the point of ``no_such_record`` is
exactly what a signed shape gives an absence that a 404 never could: it
verifies OFFLINE, against the node's own key, so "I asked and it was gone"
is citable (frozen surface's "requests are evidence").

**Caller invariance by construction.** The answer is a pure function of
``request_bytes`` — this module has no requester-identity parameter at all.
Two requesters sending byte-identical ``(subject, coverage, derivation,
page)`` (their ``nonce`` may differ) against an unchanged ledger get a
byte-identical :class:`Artifact`; only a per-request field (``nonce``,
buried inside a *refusal*'s ``request_digest``) ever varies, and it never
reaches artifact content.

**Caps and paging.** A ``range`` subject can name an arbitrarily large
selection — a full-ledger selector against a long-lived ledger is a
one-request memory/CPU amplifier otherwise. ``answer()`` never returns more
than :data:`MAX_PAGE_SIZE` bundles for one ``range`` request, and defaults
to :data:`DEFAULT_PAGE_SIZE` when the requester names no ``page.size`` of
its own. When more of the selection remains, the :class:`Artifact` carries
``next_page_token`` — an opaque string the requester echoes back verbatim
as ``request.page.token`` to fetch the next slice; its absence means the
selection ended, not that the door refused to page. A ``record`` or
``chain_segment`` subject never produces more than one answer object, so a
``page`` field on those requests is accepted and ignored. ``record``/
``range`` subjects a stranger drills into by capsule id; ``correlation``
(``{kind: "correlation", by: "nonce" | "exchange_id" | "counterparty",
value}``) asks instead about a CORRELATOR this node's own records carry —
every record (both halves this node holds for a shared ``nonce``, every
exchange naming a ``counterparty``, and every adjudication whose verdict
names one) that matches resolves the same way a ``range`` selection does:
one bundle per matched record, capped and paged identically (see
:func:`_correlation_capsule_ids`). Correlator fields are free-form
``compute_attestation`` extension data — never a fixed, registered schema
position (a producer namespaces them however it likes, e.g. under an
``x-mesh-poc-v1`` block) — so resolution walks the record for the
matching key(s) wherever a producer put them, rather than assuming one
path.

**Checkpoint writes are pull-only by default.** A ``min_freshness``
request against a stale/uncovered subject can only make ``answer()`` call
:func:`capsule_emit.witness.push` — a WRITE — when BOTH the requester
supplied a ``deadline`` (licensing the work) AND the responding node opted
in via ``allow_forced_checkpoint=True``. The node-side opt-in defaults to
``False``: an unconfigured door never writes in response to a read, no
matter what a requester asks for.

**Profile and epistemic type — additive, non-behavioral.** A requester may
name which evidence :data:`PROFILE`-taxonomy value (``outcome``,
``obligation``, ``process``, ``quality``, ``human_role``, ``attribution``,
``settlement``) its request concerns via an optional top-level
``request.profile`` field. Same caller-invariance rule as ``nonce``:
``profile`` never changes which bundles a subject resolves to or whether the
answer is an :class:`Artifact` or a :class:`Refusal` — it is echoed back on
whichever answer results (``Artifact.profile`` / ``Refusal.profile``, both
``None`` when the requester named none) purely so the answer is
self-describing about what it was asked about. Every :class:`Artifact` also
carries :attr:`Artifact.epistemic_types` — one of the eight
:data:`EPISTEMIC_TYPES` values per bundle, naming what KIND of claim each
returned bundle is (never a truth judgment, just a classification) — derived
from content this ledger already carries (an adjudication capsule's
``chain.relation``, same signal :func:`capsule_emit.adjudication
.seal_adjudication` stamps), never a new sealed field. A refusal carries no
evidence, so it carries no epistemic type.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "REASON_REQUEST_MALFORMED",
    "REASON_COVERAGE_UNSATISFIABLE",
    "REASON_NO_SUCH_RECORD",
    "REFUSAL_REASONS",
    "SUBJECT_KINDS",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PROFILE_OUTCOME",
    "PROFILE_OBLIGATION",
    "PROFILE_PROCESS",
    "PROFILE_QUALITY",
    "PROFILE_HUMAN_ROLE",
    "PROFILE_ATTRIBUTION",
    "PROFILE_SETTLEMENT",
    "PROFILES",
    "EPISTEMIC_TYPE_OBSERVED_EVENT",
    "EPISTEMIC_TYPE_SYSTEM_OF_RECORD_FACT",
    "EPISTEMIC_TYPE_PRODUCER_CLAIM",
    "EPISTEMIC_TYPE_HUMAN_REPORT",
    "EPISTEMIC_TYPE_SEMANTIC_JUDGMENT",
    "EPISTEMIC_TYPE_DERIVED_METRIC",
    "EPISTEMIC_TYPE_ADJUDICATION",
    "EPISTEMIC_TYPE_OBLIGATION_REFERENCE",
    "EPISTEMIC_TYPES",
    "RequestMalformedError",
    "RequestMap",
    "Artifact",
    "Refusal",
    "parse_request",
    "answer",
    "verify_refusal_offline",
]

REASON_REQUEST_MALFORMED = "request_malformed"
REASON_COVERAGE_UNSATISFIABLE = "coverage_unsatisfiable"
REASON_NO_SUCH_RECORD = "no_such_record"  # the wire's "recorded_absence"
REFUSAL_REASONS = frozenset(
    {REASON_REQUEST_MALFORMED, REASON_COVERAGE_UNSATISFIABLE, REASON_NO_SUCH_RECORD}
)

SUBJECT_KINDS = frozenset({"record", "range", "chain_segment", "correlation"})

#: The seven-value evidence-profile taxonomy a request's optional ``profile``
#: field may name. Advisory only (see the module docstring's "Profile and
#: epistemic type" section) -- this module never branches on it.
PROFILE_OUTCOME = "outcome"
PROFILE_OBLIGATION = "obligation"
PROFILE_PROCESS = "process"
PROFILE_QUALITY = "quality"
PROFILE_HUMAN_ROLE = "human_role"
PROFILE_ATTRIBUTION = "attribution"
PROFILE_SETTLEMENT = "settlement"
PROFILES = frozenset(
    {
        PROFILE_OUTCOME,
        PROFILE_OBLIGATION,
        PROFILE_PROCESS,
        PROFILE_QUALITY,
        PROFILE_HUMAN_ROLE,
        PROFILE_ATTRIBUTION,
        PROFILE_SETTLEMENT,
    }
)

#: The eight-value epistemic-type taxonomy an :class:`Artifact` classifies
#: each of its bundles into (one entry per bundle, see
#: :attr:`Artifact.epistemic_types`). This module only ever produces
#: PRODUCER_CLAIM, ADJUDICATION, or DERIVED_METRIC -- capsule-emit's own
#: ledger has no structural signal for the other five (e.g. distinguishing a
#: requester-observed half from a provider-claimed one is
#: capsule-emit-mesh's finer-grained model, not this repo's); the full set
#: is named here so a caller validating against "one of the eight" never
#: has to reach into a second module for the closed set.
EPISTEMIC_TYPE_OBSERVED_EVENT = "observed_event"
EPISTEMIC_TYPE_SYSTEM_OF_RECORD_FACT = "system_of_record_fact"
EPISTEMIC_TYPE_PRODUCER_CLAIM = "producer_claim"
EPISTEMIC_TYPE_HUMAN_REPORT = "human_report"
EPISTEMIC_TYPE_SEMANTIC_JUDGMENT = "semantic_judgment"
EPISTEMIC_TYPE_DERIVED_METRIC = "derived_metric"
EPISTEMIC_TYPE_ADJUDICATION = "adjudication"
EPISTEMIC_TYPE_OBLIGATION_REFERENCE = "obligation_reference"
EPISTEMIC_TYPES = frozenset(
    {
        EPISTEMIC_TYPE_OBSERVED_EVENT,
        EPISTEMIC_TYPE_SYSTEM_OF_RECORD_FACT,
        EPISTEMIC_TYPE_PRODUCER_CLAIM,
        EPISTEMIC_TYPE_HUMAN_REPORT,
        EPISTEMIC_TYPE_SEMANTIC_JUDGMENT,
        EPISTEMIC_TYPE_DERIVED_METRIC,
        EPISTEMIC_TYPE_ADJUDICATION,
        EPISTEMIC_TYPE_OBLIGATION_REFERENCE,
    }
)

#: A ``range``/``correlation`` answer never exceeds this many bundles absent
#: an explicit ``page.size`` — the bound that keeps a full-ledger selector
#: from being a one-request memory/CPU amplifier.
DEFAULT_PAGE_SIZE = 50

#: The hard ceiling on ``page.size`` — a requester cannot ask its way past
#: this by naming a larger size.
MAX_PAGE_SIZE = 200

#: ``subject.by`` values a ``correlation`` subject accepts.
CORRELATION_BY_VALUES = frozenset({"nonce", "exchange_id", "counterparty"})

#: Which ``compute_attestation``-nested key name(s) carry each correlator.
#: More than one alias exists per correlator because it is free-form
#: extension data, not a fixed schema position — different producers (e.g.
#: a Rust host-side envelope vs. the Python sidecar's ``x-mesh-poc-v1``
#: block) have named the same concept differently.
_CORRELATION_KEY_ALIASES: dict[str, frozenset[str]] = {
    "nonce": frozenset({"nonce", "client_nonce"}),
    "exchange_id": frozenset({"exchange_id"}),
    "counterparty": frozenset({"counterparty_ref"}),
}


class RequestMalformedError(RuntimeError):
    """A request map failed to parse — caught by :func:`answer` and turned
    into a signed ``request_malformed`` refusal; never raised past it."""


@dataclass(frozen=True)
class RequestMap:
    """The parsed request — see the module docstring for the wire shape.

    ``has_deadline`` is a bool, not the deadline's value: its PRESENCE is
    one of two conditions :func:`answer` requires to call ``push()`` to
    satisfy ``min_freshness`` (the requester is asking for fresh-enough
    evidence and accepting the work that takes) — the other is the
    responding node's own ``allow_forced_checkpoint`` opt-in, which
    defaults off; the deadline's own value is a transport-level concern
    (how long the requester will wait), not this module's.

    ``page`` is ``{token?: str, size?: int}`` — see the module docstring's
    "Caps and paging" note. Only a ``range`` subject reads it; present but
    unused for ``record``/``chain_segment``.

    ``profile`` is one of :data:`PROFILES` or ``None`` — see the module
    docstring's "Profile and epistemic type" section. Same caller-invariance
    treatment as ``nonce``: carried through to the answer, never read by
    :func:`_build_bundles` or anything else that decides what gets returned.
    """

    subject: dict
    coverage: dict
    derivation: str | None
    has_deadline: bool
    nonce: str | None
    page: dict
    profile: str | None


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RequestMalformedError(message)


def _is_size_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid_chain_segment_subject(subject: dict) -> bool:
    """``{last: <positive int>}`` XOR ``{from_size, to_size}`` (each a
    non-negative int, ``to_size >= from_size``) — never both, never
    neither. Mirrors the exact validation
    :func:`capsule_emit.chain_segment.chain_segment` itself enforces, so a
    malformed shape is caught here as ``request_malformed`` rather than
    surfacing as a ``chain_segment.ChainSegmentError`` deep in
    ``_build_bundles``."""
    has_last = "last" in subject
    has_range = "from_size" in subject or "to_size" in subject
    if has_last and has_range:
        return False
    if has_last:
        last = subject["last"]
        return isinstance(last, int) and not isinstance(last, bool) and last > 0
    if has_range:
        return (
            "from_size" in subject
            and "to_size" in subject
            and _is_size_int(subject["from_size"])
            and _is_size_int(subject["to_size"])
            and subject["to_size"] >= subject["from_size"]
        )
    return False


def parse_request(request_bytes: bytes) -> RequestMap:
    """Parse ``request_bytes`` into a :class:`RequestMap`.

    Raises :class:`RequestMalformedError` for anything that is not a JSON
    object shaped like ``{subject, coverage?, derivation?, deadline?,
    nonce?, profile?}`` — unknown fields are ignored, per the draft's shape.
    ``profile``, when present, must be one of :data:`PROFILES`.
    """
    try:
        data = json.loads(request_bytes)
    except Exception as exc:  # noqa: BLE001 — any parse failure is malformed
        raise RequestMalformedError(f"request is not valid JSON: {exc}") from exc
    _require(isinstance(data, dict), "request must be a JSON object")

    subject = data.get("subject")
    _require(isinstance(subject, dict), "subject is required and must be an object")
    kind = subject.get("kind")
    _require(isinstance(kind, str) and kind in SUBJECT_KINDS, f"subject.kind must be one of {sorted(SUBJECT_KINDS)}")
    if kind == "record":
        _require(isinstance(subject.get("capsule_id"), str) and subject["capsule_id"], "subject.kind='record' requires a non-empty subject.capsule_id")
    elif kind == "range":
        _require(isinstance(subject.get("selector"), str) and subject["selector"], "subject.kind='range' requires a non-empty subject.selector")
    elif kind == "correlation":
        by = subject.get("by")
        _require(
            isinstance(by, str) and by in CORRELATION_BY_VALUES,
            f"subject.kind='correlation' requires subject.by to be one of {sorted(CORRELATION_BY_VALUES)}",
        )
        _require(isinstance(subject.get("value"), str) and subject["value"], "subject.kind='correlation' requires a non-empty subject.value")
    else:  # "chain_segment"
        _require(
            _valid_chain_segment_subject(subject),
            "subject.kind='chain_segment' requires either {last: <positive int>} or "
            "{from_size: <non-negative int>, to_size: <int >= from_size>} — never both",
        )

    coverage = data.get("coverage") or {}
    _require(isinstance(coverage, dict), "coverage must be an object")
    if "expected_pin" in coverage:
        pin = coverage["expected_pin"]
        _require(
            isinstance(pin, dict) and isinstance(pin.get("root"), str) and isinstance(pin.get("mmr_size"), int),
            "coverage.expected_pin requires root (string) and mmr_size (int)",
        )
    if "min_freshness" in coverage:
        mf = coverage["min_freshness"]
        _require(
            isinstance(mf, dict) and isinstance(mf.get("max_age_seconds"), (int, float)) and not isinstance(mf.get("max_age_seconds"), bool),
            "coverage.min_freshness requires max_age_seconds (a number)",
        )

    derivation = data.get("derivation")
    _require(derivation is None or isinstance(derivation, str), "derivation must be a string when present")

    nonce = data.get("nonce")
    _require(nonce is None or isinstance(nonce, str), "nonce must be a string when present")

    profile = data.get("profile")
    _require(
        profile is None or (isinstance(profile, str) and profile in PROFILES),
        f"profile must be one of {sorted(PROFILES)} when present",
    )

    page = data.get("page") or {}
    _require(isinstance(page, dict), "page must be an object")
    if "token" in page:
        token = page["token"]
        _require(
            isinstance(token, str) and token.isdigit(),
            "page.token must be a token this door itself issued as a prior answer's next_page_token",
        )
    if "size" in page:
        size = page["size"]
        _require(
            isinstance(size, int) and not isinstance(size, bool) and size > 0,
            "page.size must be a positive int",
        )

    return RequestMap(
        subject=subject,
        coverage=coverage,
        derivation=derivation,
        has_deadline=data.get("deadline") is not None,
        nonce=nonce,
        page=page,
        profile=profile,
    )


@dataclass(frozen=True)
class Artifact:
    """One well-formed answer — one ``Bundle`` for a ``record`` subject, one
    per selected/matched record for a ``range`` or ``correlation`` subject.
    Digests-only, always: this is the SAME object a stranger and a trusted
    counterparty both receive for the same request.

    ``profile`` echoes the request's ``profile`` (``None`` if the requester
    named none) — see the module docstring's "Profile and epistemic type"
    section. ``epistemic_types`` names one :data:`EPISTEMIC_TYPES` value per
    entry in ``bundles`` (same length, same order) — what KIND of claim each
    bundle is, never whether it is true.
    """

    v: int
    subject_kind: str
    bundles: tuple[Any, ...]  # capsule_emit.bundle.Bundle, one or more
    next_page_token: str | None = None
    profile: str | None = None
    epistemic_types: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "v": self.v,
            "subject_kind": self.subject_kind,
            "bundles": [b.to_dict() for b in self.bundles],
            "epistemic_types": list(self.epistemic_types),
        }
        if self.next_page_token is not None:
            d["next_page_token"] = self.next_page_token
        if self.profile is not None:
            d["profile"] = self.profile
        return d


@dataclass(frozen=True)
class Refusal:
    """A signed decline or recorded absence — verifies OFFLINE, never an
    unsigned 404. ``reason == REASON_NO_SUCH_RECORD`` is the wire's
    "recorded_absence"; any other reason is a policy-shaped
    "signed_refusal" — same object either way.

    ``profile`` echoes the request's ``profile`` (``None`` if the requester
    named none) — an unsigned convenience field, same as ``request_digest``
    already implies for ``nonce``: a verifier holding the original request
    bytes can always recompute ``request_digest`` to cross-check any echoed
    field, so leaving ``profile`` out of :meth:`signing_body` matches how
    every other request-only field (e.g. ``nonce``) is already handled — it
    is not itself re-signed, only the request it came from is.
    """

    request_digest: str
    reason: str
    issued_at: str
    key_id: str
    sig: str
    profile: str | None = None

    def signing_body(self) -> bytes:
        """Canonical bytes the signature covers — same three fields the
        wire names, JSON with sorted keys so the body is reconstructible
        from the object alone (no separate canonicalization import needed
        by an offline verifier)."""
        return json.dumps(
            {"request_digest": self.request_digest, "reason": self.reason, "issued_at": self.issued_at},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def to_dict(self) -> dict:
        d = {
            "request_digest": self.request_digest,
            "reason": self.reason,
            "issued_at": self.issued_at,
            "key_id": self.key_id,
            "sig": self.sig,
        }
        if self.profile is not None:
            d["profile"] = self.profile
        return d


def verify_refusal_offline(refusal: Refusal) -> bool:
    """Pure, offline, no-network check that ``refusal.sig`` was produced by
    the holder of ``refusal.key_id`` over this refusal's own signing body —
    same reconstruct-the-public-key-from-``key_id`` convention as
    ``capsule_emit.checkpoint.verify_checkpoint_signature_offline``. Never
    raises; any malformed input is a verification failure, not an
    exception."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(refusal.key_id))
        public_key.verify(bytes.fromhex(refusal.sig), refusal.signing_body())
        return True
    except Exception:  # noqa: BLE001 — pure verifier, never raises
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refuse(
    request_digest: str, reason: str, *, signer: Any, issued_at: str, profile: str | None = None
) -> Refusal:
    stub = Refusal(request_digest=request_digest, reason=reason, issued_at=issued_at, key_id="", sig="")
    sig, key_id = signer.sign(stub.signing_body())
    return Refusal(
        request_digest=request_digest,
        reason=reason,
        issued_at=issued_at,
        key_id=key_id,
        sig=sig,
        profile=profile,
    )


def _epistemic_type_for_bundle(b: Any) -> str:
    """Classify one bundle's epistemic type from content this ledger
    already carries — never a new sealed field (see the module docstring's
    "Profile and epistemic type" section).

    An adjudication capsule's ``chain.relation`` is ``"adjudicates"`` (the
    same value :data:`capsule_emit.adjudication.RELATION_ADJUDICATES`
    defines — duplicated here as a literal rather than imported, the same
    choice :func:`_entry_names_counterparty_in_adjudication` above already
    makes for the ``"contradicted:"`` prefix: this module stays a leaf
    reader of ledger content, never a caller of the sealing module). Every
    other capsule this ledger seals is this producer's own claim about what
    it did — :data:`EPISTEMIC_TYPE_PRODUCER_CLAIM` — since a plain ``seal()``
    capsule carries no structural signal distinguishing a requester-observed
    half from a provider-claimed one (capsule-emit-mesh's producer/requester
    split is a finer grain this repo's own ledger does not have).
    """
    chain = b.receipt.get("chain") or {}
    if chain.get("relation") == "adjudicates":
        return EPISTEMIC_TYPE_ADJUDICATION
    return EPISTEMIC_TYPE_PRODUCER_CLAIM


def _record_exists(entries: list[dict], capsule_id: str) -> bool:
    """Same matching predicate ``cll.checkpoint.bundle``'s internal
    ``_find_record`` uses (full id, or an unambiguous >=8-char prefix;
    checkpoint-stamp/disclosure bookkeeping entries never match) — kept
    local rather than reaching into that package's private helper, since
    all this needs is existence, not ``bundle()``'s own ambiguous-prefix
    diagnostics."""
    from .ledger import NON_CAPSULE_KINDS

    return any(
        e.get("kind") not in NON_CAPSULE_KINDS
        and (
            e.get("capsule_id") == capsule_id
            or (len(capsule_id) >= 8 and str(e.get("capsule_id", "")).startswith(capsule_id))
        )
        for e in entries
    )


def _range_capsule_ids(ledger: Any, selector: str) -> list[str] | None:
    """Resolve a ``range`` subject's selector via the SAME syntax
    :mod:`capsule_emit.disclose` parses (``id1..id2`` / ``id1,id2,...``) —
    reused, not reimplemented. Returns ``None`` when the selector cannot be
    resolved (no such records) rather than raising, since that is this
    module's ``no_such_record`` case, not a malformed request."""
    from .disclose import DiscloseError, _resolve_selector
    from .ledger import read_ledger

    records = read_ledger(ledger)
    if not records:
        return None
    try:
        selected, _mode = _resolve_selector(records, selector)
    except DiscloseError:
        return None
    return [r["capsule_id"] for r in selected]


def _iter_values_by_key(obj: Any, keys: frozenset[str]) -> Iterator[str]:
    """Recursively walk *obj* (a JSON-decoded capsule record — nested
    dicts/lists only) and yield every string value found under a key in
    *keys*, at any depth. Correlator fields are free-form
    ``compute_attestation`` extension data (see the module docstring), so
    this finds the value wherever a producer nested it rather than
    assuming one fixed path."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                yield v
            yield from _iter_values_by_key(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_values_by_key(item, keys)


def _iter_dicts_by_key(obj: Any, key: str) -> Iterator[dict]:
    """Like :func:`_iter_values_by_key`, but yields dict VALUES found under
    *key* at any depth — used to find the ``adjudication`` block wherever
    it is nested (``model_attestation.compute_attestation.adjudication``
    today; this module never hardcodes that path — see the module
    docstring)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, dict):
                yield v
            yield from _iter_dicts_by_key(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dicts_by_key(item, key)


def _entry_names_counterparty_in_adjudication(entry: dict, value: str) -> bool:
    """``counterparty`` correlation also matches an adjudication capsule
    whose verdict names *value* as the divergent owner
    (``"contradicted:<owner_id>"`` — see
    :func:`capsule_emit.adjudication.seal_adjudication`) — the one place an
    adjudication record names a counterparty, since ``corroborated``/
    ``inconclusive`` verdicts name no owner at all."""
    return any(
        adjudication.get("verdict") == f"contradicted:{value}"
        for adjudication in _iter_dicts_by_key(entry, "adjudication")
    )


def _entry_matches_correlation(entry: dict, by: str, value: str) -> bool:
    if by == "counterparty" and _entry_names_counterparty_in_adjudication(entry, value):
        return True
    return any(v == value for v in _iter_values_by_key(entry, _CORRELATION_KEY_ALIASES[by]))


def _correlation_capsule_ids(ledger: Any, by: str, value: str) -> list[str] | None:
    """Resolve a ``correlation`` subject to every capsule_id in this ledger
    carrying *value* under *by*'s correlator field(s), in ledger (append)
    order — same ordering :func:`_range_capsule_ids` uses, so paging is
    stable. Returns ``None`` when nothing matches (this subject's
    ``no_such_record`` case, not a malformed request)."""
    from .ledger import read_ledger

    records = read_ledger(ledger)
    matched = [r["capsule_id"] for r in records if _entry_matches_correlation(r, by, value)]
    return matched or None


def _build_chain_segment(
    ledger: Any, req: RequestMap, *, self_owner_id: str | None
) -> tuple[tuple[Any, ...] | None, str | None, str | None]:
    """The ``chain_segment`` leg of :func:`_build_bundles` — split out since
    it dispatches to :mod:`capsule_emit.chain_segment` over the checkpoint
    CHAIN, not to :func:`capsule_emit.bundle.bundle` over individual
    records. Returns the same ``(bundles, reason, next_page_token)`` shape
    :func:`_build_bundles` does — ``next_page_token`` is always ``None``
    here: a chain segment is O(checkpoints), not O(records), so it is
    cheap by construction and never paged (see the module docstring)."""
    from .chain_segment import ChainSegmentError
    from .chain_segment import chain_segment as _chain_segment_fn
    from .ledger import read_ledger_entries

    entries = read_ledger_entries(ledger)
    if not entries:
        return None, REASON_NO_SUCH_RECORD, None

    subject = req.subject
    try:
        segment = _chain_segment_fn(
            entries,
            from_size=subject.get("from_size"),
            to_size=subject.get("to_size"),
            last=subject.get("last"),
            self_owner_id=self_owner_id,
            leaf_digests=bool(subject.get("leaf_digests", False)),
        )
    except ChainSegmentError:
        return None, REASON_COVERAGE_UNSATISFIABLE, None
    return (segment,), None, None


def _page_slice(capsule_ids: list[str], page: dict) -> tuple[list[str], str | None]:
    """Slice ``capsule_ids`` (already resolved, in ledger order) to the
    requested page — see the module docstring's "Caps and paging" note.
    ``page.token`` (validated numeric-string by :func:`parse_request`) is
    the offset into ``capsule_ids`` the previous answer stopped at;
    ``page.size`` (validated positive int) overrides
    :data:`DEFAULT_PAGE_SIZE`, capped at :data:`MAX_PAGE_SIZE` either way.
    Returns ``(this_page_ids, next_page_token)`` — ``next_page_token`` is
    ``None`` exactly when this page reaches the end of ``capsule_ids``.
    """
    offset = int(page["token"]) if "token" in page else 0
    size = min(page.get("size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
    offset = min(offset, len(capsule_ids))
    page_ids = capsule_ids[offset : offset + size]
    next_offset = offset + size
    next_token = str(next_offset) if next_offset < len(capsule_ids) else None
    return page_ids, next_token


def _build_bundles(
    ledger: Any, req: RequestMap, *, self_owner_id: str | None = None
) -> tuple[tuple[Any, ...] | None, str | None, str | None]:
    """Attempt to build the bundles this request's subject names.

    Returns ``(bundles, None, next_page_token)`` on success, or ``(None,
    reason, None)`` naming which refusal reason applies —
    ``no_such_record`` (the subject names nothing this node ever sealed) or
    ``coverage_unsatisfiable`` (the subject exists but is not yet covered
    by any checkpoint). ``next_page_token`` is non-``None`` only for a
    ``range`` subject whose resolved selection exceeds one page.
    """
    from .bundle import BundleError
    from .bundle import bundle as _bundle_fn
    from .ledger import read_ledger_entries

    kind = req.subject["kind"]
    if kind == "chain_segment":
        return _build_chain_segment(ledger, req, self_owner_id=self_owner_id)
    next_page_token = None
    if kind == "record":
        capsule_ids = [req.subject["capsule_id"]]
    elif kind == "range":
        resolved = _range_capsule_ids(ledger, req.subject["selector"])
        if not resolved:
            return None, REASON_NO_SUCH_RECORD, None
        capsule_ids, next_page_token = _page_slice(resolved, req.page)
    else:  # "correlation"
        resolved = _correlation_capsule_ids(ledger, req.subject["by"], req.subject["value"])
        if not resolved:
            return None, REASON_NO_SUCH_RECORD, None
        capsule_ids, next_page_token = _page_slice(resolved, req.page)

    entries = read_ledger_entries(ledger)
    if not entries or not all(_record_exists(entries, cid) for cid in capsule_ids):
        return None, REASON_NO_SUCH_RECORD, None

    try:
        bundles = tuple(_bundle_fn(ledger, cid) for cid in capsule_ids)
    except BundleError:
        # The record(s) exist but at least one isn't covered by any
        # checkpoint yet — a coverage lag, not an absence.
        return None, REASON_COVERAGE_UNSATISFIABLE, None
    return bundles, None, next_page_token


def _checkpoint_age_seconds(timestamp: str, issued_at: str) -> float:
    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    return (now - ts).total_seconds()


def answer(
    request_bytes: bytes,
    *,
    ledger: Any,
    signer: Any = None,
    signing_key_path: Any = None,
    now: str | None = None,
    allow_forced_checkpoint: bool = False,
) -> Artifact | Refusal:
    """The one evidence-request responder.

    Parses ``request_bytes`` (the wire's request map), resolves
    ``coverage``, and dispatches (via :func:`_build_bundles`) to
    :func:`capsule_emit.bundle.bundle` for the record(s) a ``record``/
    ``range``/``correlation`` subject names, or to
    :func:`capsule_emit.chain_segment.chain_segment` for a
    ``chain_segment`` subject. Returns exactly one of:

      * :class:`Artifact` — one or more offline-verifiable ``Bundle``/
        ``ChainSegment`` objects, each classified into one of
        ``EPISTEMIC_TYPES`` (:attr:`Artifact.epistemic_types`);
      * :class:`Refusal` — signed, offline-verifiable, one of
        ``REFUSAL_REASONS``.

    Both echo the request's ``profile`` (see the module docstring's
    "Profile and epistemic type" section) — ``None`` when the requester
    named none.

    ``signer``/``signing_key_path`` resolve to the SAME producer key
    ``seal()`` uses for this ledger (``capsule_emit.signing.resolve_signer``)
    — ``issuer: node_key``, the same identity every capsule on this ledger
    is already signed with.

    ``now`` overrides the wall clock (for deterministic tests); defaults to
    the real UTC time.

    ``allow_forced_checkpoint`` is this NODE's own policy opt-in (default
    ``False``) — see the module docstring's "Checkpoint writes are
    pull-only by default" note. A requester's ``deadline`` alone never
    forces a write; both sides must agree.
    """
    import os

    from . import signing as _signing
    from . import witness as _witness

    request_digest = _digest(request_bytes)
    issued_at = now or _now_iso()
    signer_obj = _signing.resolve_signer(os.fspath(ledger), signer=signer, key_path=signing_key_path)

    try:
        req = parse_request(request_bytes)
    except RequestMalformedError:
        return _refuse(request_digest, REASON_REQUEST_MALFORMED, signer=signer_obj, issued_at=issued_at)

    bundles, reason, next_page_token = _build_bundles(ledger, req, self_owner_id=signer_obj.key_id)

    min_freshness = req.coverage.get("min_freshness")
    may_force_checkpoint = req.has_deadline and allow_forced_checkpoint
    if bundles is None and reason == REASON_COVERAGE_UNSATISFIABLE and min_freshness and may_force_checkpoint:
        # min_freshness licenses doing the work under a deadline, and this
        # node has opted in to doing it: force a checkpoint now and retry
        # once.
        _witness.push(os.fspath(ledger), signer=signer_obj)
        bundles, reason, next_page_token = _build_bundles(ledger, req, self_owner_id=signer_obj.key_id)

    if bundles is None:
        return _refuse(request_digest, reason, signer=signer_obj, issued_at=issued_at, profile=req.profile)

    expected_pin = req.coverage.get("expected_pin")
    if expected_pin is not None:
        pin_matches = all(
            b.checkpoint.mmr_size == expected_pin["mmr_size"] and b.checkpoint.root == expected_pin["root"]
            for b in bundles
        )
        if not pin_matches:
            # The requester pinned a checkpoint that does not (or no
            # longer) covers this subject — refuse rather than silently
            # serve under a different anchor than the one asked for.
            return _refuse(
                request_digest,
                REASON_COVERAGE_UNSATISFIABLE,
                signer=signer_obj,
                issued_at=issued_at,
                profile=req.profile,
            )
    elif min_freshness is not None:
        max_age = min_freshness["max_age_seconds"]
        stale = any(_checkpoint_age_seconds(b.checkpoint.timestamp, issued_at) > max_age for b in bundles)
        if stale:
            if may_force_checkpoint:
                _witness.push(os.fspath(ledger), signer=signer_obj)
                bundles, reason, next_page_token = _build_bundles(ledger, req, self_owner_id=signer_obj.key_id)
                if bundles is None:
                    return _refuse(
                        request_digest, reason, signer=signer_obj, issued_at=issued_at, profile=req.profile
                    )
                stale = any(_checkpoint_age_seconds(b.checkpoint.timestamp, issued_at) > max_age for b in bundles)
            if stale:
                return _refuse(
                    request_digest,
                    REASON_COVERAGE_UNSATISFIABLE,
                    signer=signer_obj,
                    issued_at=issued_at,
                    profile=req.profile,
                )

    if req.subject["kind"] == "chain_segment":
        epistemic_types = (EPISTEMIC_TYPE_DERIVED_METRIC,) * len(bundles)
    else:
        epistemic_types = tuple(_epistemic_type_for_bundle(b) for b in bundles)

    return Artifact(
        v=1,
        subject_kind=req.subject["kind"],
        bundles=bundles,
        next_page_token=next_page_token,
        profile=req.profile,
        epistemic_types=epistemic_types,
    )
