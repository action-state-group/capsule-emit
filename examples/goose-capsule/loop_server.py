#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Goose extension for the 12-step loop: the steps a Goose agent can run as tools.

``server.py`` seals each tool call (step 01). This extension adds the tools for
the other steps whose library calls exist in capsule-emit, one tool per step
verb, mapped one to one against the Goose column of evidence-network/loop#154:

    step  tool(s)                                   status for Goose
    ----  ----------------------------------------  ------------------------------
    01    capsule_record (capsule_emit.server)      maps
    02    loop_book                                 maps when a witness receipt verifies,
                                                    else the result says NOT_CHECKED
    03    none                                      NOT_CHECKED (no commitment publisher)
    04    loop_admit                                maps (peer GO/NO sealed before a deal)
    05    loop_deal_open / loop_deal_respond /      maps (each side seals its half, pushes
          loop_deal_push / loop_deal_reconcile       it as a file, cites the other's)
    06    loop_settle / loop_settle_verify          maps for the draft's legs; no rail is
                                                    driven (the caller supplies the reference)
    07    none                                      NOT_CHECKED (stdio cannot host a responder)
    08    capsule_verify, capsule-emit bundle       maps (loop-owned)
    09    none                                      NOT_CHECKED (no delivery into a party's book)
    10/11 the loop's own pack and page              maps (loop-owned)
    12    loop_join / loop_probe                    maps for the book; the commitment (03)
                                                    is absent

Nothing here moves money or opens a connection of its own. Sealing and loop_book's
checkpoint_now follow capsule-emit's witness settings (CAPSULE_WITNESS, CAPSULE_WITNESS_URL)
like every capsule-emit call; set CAPSULE_WITNESS=off for none. A payment reference is a
value the caller passes in.

Add to ~/.config/goose/config.yaml:

    extensions:
      capsule_loop:
        enabled: true
        type: stdio
        name: capsule_loop
        description: "Capsule loop steps: book, admit, deal, settle, join"
        cmd: python3
        args: ["/path/to/examples/goose-capsule/loop_server.py"]
        timeout: 30
        envs:
          CAPSULE_LEDGER: "/path/to/my-book"
          CAPSULE_OPERATOR: "acme-co"
          CAPSULE_DEVELOPER: "goose-agent@v1"

Every tool returns one JSON object. A refusal is ``{"ok": false, "error": ...}``;
a step that cannot be checked says ``"NOT_CHECKED"`` and why, never a guess.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp < 2
    from mcp.server.fastmcp import FastMCP

from agent_action_capsule.canonical import jcs

from capsule_emit import read_ledger, seal
from capsule_emit import settlement as _settlement
from capsule_emit.bilateral import seal_action, seal_request
from capsule_emit.ledger import NON_CAPSULE_KINDS, read_ledger_entries, read_positioned_entries
from capsule_emit.ledger_layout import DEFAULT_STORE, LEDGER_ENV, resolve_ledger
from capsule_emit.reconciliation import fold, reconcile
from capsule_emit.verification import verify_capsule

_OPERATOR = os.environ.get("CAPSULE_OPERATOR", "goose-user")
_DEVELOPER = os.environ.get("CAPSULE_DEVELOPER", "goose-agent@v1")

GO = "GO"
NO = "NO"
NOT_CHECKED = "NOT_CHECKED"

server = FastMCP(
    "capsule-loop",
    instructions=(
        "Run the capsule loop's steps: loop_join starts a book, loop_book shows a "
        "record's position and checkpoint, loop_admit seals a GO/NO on a peer before "
        "any deal, loop_deal_* seal and exchange the two halves of a deal, loop_settle "
        "seals a settlement leg. Steps 03, 07 and 09 are NOT_CHECKED for this extension."
    ),
)


def _out(**members: Any) -> str:
    return json.dumps(members, sort_keys=True, default=str)


def _refuse(error: str) -> str:
    return _out(ok=False, error=error)


def _book_path(ledger: str | None) -> str:
    """The book to read. Reading never refuses what only writing refuses."""
    try:
        return str(resolve_ledger(ledger).path)
    except Exception:
        return str(ledger or os.environ.get(LEDGER_ENV) or DEFAULT_STORE)


def _compute(capsule: dict) -> dict:
    return (capsule.get("model_attestation") or {}).get("compute_attestation") or {}


def _capsules(ledger: str | None) -> list[dict]:
    return [r for r in read_ledger(_book_path(ledger)) if r.get("capsule_id")]


def _log_capsules(path: str) -> list[tuple[int, dict]]:
    """Each capsule with its leaf position in the log. Checkpoint stamps and padding are
    leaves too, so the position counts them; they are never returned as capsules."""
    return [
        (i, e)
        for i, e in enumerate(read_positioned_entries(path))
        if e.get("capsule_id") and e.get("kind") not in NON_CAPSULE_KINDS and e.get("record_type") != "padding"
    ]


def _read_json(path: str, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {what}: {exc}") from exc


def _capsule_list(path: str, what: str) -> list[dict]:
    data = _read_json(path, what)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError(f"{what} must hold a JSON array of capsules")
    return data


def _action_digest(action: str, terms: Any) -> str:
    return hashlib.sha256(jcs({"action": action, "terms": terms})).hexdigest()


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc


def _party(capsule: dict) -> str:
    return str(capsule.get("operator", ""))


def _admission(ledger: str | None, peer: str) -> dict | None:
    """The newest admission this book sealed for *peer*, or ``None``."""
    found = None
    for rec in _capsules(ledger):
        adm = _compute(rec).get("admission")
        if isinstance(adm, dict) and adm.get("peer") == peer:
            found = {"capsule_id": rec["capsule_id"], **adm}
    return found


def _admitted(ledger: str | None, peer: str) -> tuple[dict | None, str | None]:
    adm = _admission(ledger, peer)
    if adm is None:
        return None, f"no admission sealed for {peer!r}: call loop_admit first"
    if adm["decision"] != GO:
        return None, f"the sealed admission for {peer!r} is {adm['decision']}: no deal"
    return adm, None


# ---------------------------------------------------------------------------
# 12 Join
# ---------------------------------------------------------------------------


@server.tool()
def loop_join(
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str | None = None,
) -> str:
    """Join the loop with one call: open this node's book and seal its first record.

    Returns the book path, the join record's capsule_id and the book position.
    The node's commitment (step 03) is not published by this extension.
    """
    try:
        res = seal({"join": operator}, action="loop.join", operator=operator, developer=developer, ledger=ledger)
    except Exception as exc:
        return _refuse(f"join failed: {exc}")
    book = _book_path(ledger)
    return _out(
        ok=True,
        step="12",
        book=book,
        capsule_id=res.capsule_id,
        position=_log_capsules(book)[-1][0],
        commitment=f"{NOT_CHECKED}: step 03 has no publisher in this extension",
        probe="a stranger runs loop_probe on a copy of the book",
    )


@server.tool()
def loop_probe(ledger: str | None = None) -> str:
    """Verify every capsule in a book offline, as a stranger holding only a copy of it would.

    Nothing is fetched and nothing is written.
    """
    path = _book_path(ledger)
    try:
        gaps = _unreadable(path)
        recs = [c for _, c in _log_capsules(path)]
    except Exception as exc:
        return _refuse(f"book cannot be read: {exc}")
    failures = [{"gap": g} for g in gaps]
    for rec in recs:
        vr = verify_capsule(rec)
        if not vr.ok:
            failures.append({"capsule_id": rec["capsule_id"], "findings": [f.detail for f in vr.findings]})
    return _out(ok=not failures and bool(recs), step="12", checked=len(recs), failures=failures)


def _unreadable(path: str) -> list[str]:
    """What a tolerant read would silently drop: archived segments and corrupt lines."""
    gaps = [f"archived segment {e.get('segment')}" for e in read_ledger_entries(path) if e.get("kind") == "archived_segment"]
    p = Path(path)
    if p.is_file():
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) != len(read_ledger_entries(path)):
            gaps.append(f"{len(lines) - len(read_ledger_entries(path))} corrupt line(s)")
    return gaps


# ---------------------------------------------------------------------------
# 02 Book
# ---------------------------------------------------------------------------


@server.tool()
def loop_book(
    capsule_id: str,
    ledger: str | None = None,
    witness_directory_path: str | None = None,
    checkpoint_now: bool = False,
) -> str:
    """Where a record sits in this book, and the checkpoint that covers it.

    ``witness_receipt`` is ``verified`` only when a receipt on the latest checkpoint
    verifies under a key from *witness_directory_path* (a witnesses.json you chose);
    otherwise it is NOT_CHECKED, because the book's own witness state is the node's
    own word. *checkpoint_now* forces a checkpoint first (honors CAPSULE_WITNESS).
    """
    from capsule_emit import witness
    from capsule_emit.status import compute_status

    path = _book_path(ledger)
    if checkpoint_now:
        try:
            witness.push(path)
        except Exception as exc:
            return _refuse(f"checkpoint failed: {exc}")
    try:
        hits = [(i, c) for i, c in _log_capsules(path) if c["capsule_id"].startswith(capsule_id)]
    except Exception as exc:
        return _refuse(f"book cannot be read: {exc}")
    if len(capsule_id) < 8 or not hits:
        return _refuse(f"no record with capsule_id prefix {capsule_id!r} (minimum 8 characters) in {path}")
    if len(hits) > 1:
        return _refuse(f"capsule_id prefix {capsule_id!r} matches {len(hits)} records: give more characters")
    position, record = hits[0]
    directory = None
    if witness_directory_path:
        try:
            directory = json.loads(Path(witness_directory_path).read_text())
        except (OSError, ValueError) as exc:
            return _refuse(f"cannot read witness directory: {exc}")
    st = compute_status(path, offline=True, witness_directory=directory)
    cp = st["latest_checkpoint"]
    covered = cp is not None and position < cp["leaf_count"]
    verified = covered and cp["grade"] == "witnessed"
    return _out(
        ok=True,
        step="02",
        capsule_id=record["capsule_id"],
        position=position,
        capsule_count=st["capsule_count"],
        checkpoint=None
        if cp is None
        else {"mmr_size": cp["mmr_size"], "leaf_count": cp["leaf_count"], "timestamp": cp["timestamp"], "covers_record": covered},
        witness_receipt="verified" if verified else f"{NOT_CHECKED}: no receipt verified under a supplied key for a checkpoint covering this record",
    )


# ---------------------------------------------------------------------------
# 04 Admit
# ---------------------------------------------------------------------------


@server.tool()
def loop_admit(
    peer: str,
    decision: str,
    reason: str = "",
    peer_key_id: str = "",
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str | None = None,
) -> str:
    """Seal this node's GO or NO on a peer, before any deal with it.

    This is a peer admission, not the tool-call gate: loop_deal_open and
    loop_deal_respond refuse unless the newest admission sealed for the peer is GO.
    The peer is named by its operator string; pass *peer_key_id* (the key_id its capsules
    carry) to bind the admission to a signing key, which loop_deal_respond then requires.
    """
    decision = decision.strip().upper()
    if decision not in (GO, NO):
        return _refuse("decision must be GO or NO")
    if not peer:
        return _refuse("peer is required")
    try:
        res = seal(
            {"peer": peer, "decision": decision, "reason": reason},
            action="loop.admit",
            action_type="decide",
            operator=operator,
            developer=developer,
            verdict="executed" if decision == GO else "blocked",
            extra_compute={"admission": {"peer": peer, "decision": decision, **({"peer_key_id": peer_key_id} if peer_key_id else {})}},
            ledger=ledger,
        )
    except Exception as exc:
        return _refuse(f"admit failed: {exc}")
    return _out(ok=True, step="04", capsule_id=res.capsule_id, peer=peer, decision=decision)


# ---------------------------------------------------------------------------
# 05 Deal and push
# ---------------------------------------------------------------------------


@server.tool()
def loop_deal_open(
    peer: str,
    action: str,
    terms: str,
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str | None = None,
) -> str:
    """Seal this node's half of a deal (the request) with an admitted peer.

    *terms* is a JSON document; both halves commit to the digest of (action, terms).
    Refused unless the newest admission for *peer* is GO.
    """
    adm, err = _admitted(ledger, peer)
    if err:
        return _refuse(err)
    try:
        digest = _action_digest(action, _json(terms, "terms"))
        res = seal_request(
            operator, developer, action, digest, ledger=ledger,
            extra_compute={"peer": peer, "admission_capsule_id": adm["capsule_id"]},
        )
    except Exception as exc:
        return _refuse(f"deal open failed: {exc}")
    return _out(ok=True, step="05", capsule_id=res.capsule_id, action_digest=digest, admission=adm["capsule_id"])


@server.tool()
def loop_deal_respond(
    request_file: str,
    action: str,
    terms: str,
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str | None = None,
) -> str:
    """Seal this node's half in answer to a peer's pushed request, citing it.

    The request must verify, name this node's own terms by digest, and come from a
    peer this node has admitted with GO.
    """
    try:
        req = _read_json(request_file, "request")
    except ValueError as exc:
        return _refuse(str(exc))
    if isinstance(req, dict):
        req = [req]
    if not isinstance(req, list) or not all(isinstance(r, dict) for r in req):
        return _refuse("the request file must hold a capsule or a JSON array of capsules")
    reqs = [r for r in req if _compute(r).get("role") == "requester"]
    if len(reqs) != 1:
        return _refuse("the request file must hold exactly one request half")
    req = reqs[0]
    if not verify_capsule(req).ok:
        return _refuse("the pushed request does not verify")
    if _compute(req).get("peer") != operator:
        return _refuse(f"the request is addressed to {_compute(req).get('peer')!r}, not {operator!r}")
    adm, err = _admitted(ledger, _party(req))
    if err:
        return _refuse(err)
    if adm.get("peer_key_id") and adm["peer_key_id"] != req.get("key_id"):
        return _refuse("the request is not signed by the key this node admitted for that peer")
    try:
        digest = _action_digest(action, _json(terms, "terms"))
    except ValueError as exc:
        return _refuse(str(exc))
    if _compute(req).get("action_digest") != digest:
        return _refuse("the request commits to different terms than the ones given")
    try:
        res = seal_action(
            operator, developer, action, digest, req["capsule_id"], ledger=ledger,
            extra_compute={"peer": _party(req), "admission_capsule_id": adm["capsule_id"], "request_capsule_id": req["capsule_id"]},
        )
    except Exception as exc:
        return _refuse(f"deal respond failed: {exc}")
    return _out(ok=True, step="05", capsule_id=res.capsule_id, action_digest=digest, cites=req["capsule_id"])


@server.tool()
def loop_deal_push(capsule_ids: list[str], out_path: str, ledger: str | None = None) -> str:
    """Write this node's sealed deal halves to a file to hand to the counterparty.

    Writes a local file only; moving it to the peer is the caller's channel.
    """
    recs = _capsules(ledger)
    chosen = []
    for want in capsule_ids:
        hit = next((r for r in recs if r["capsule_id"] == want), None)
        if hit is None:
            return _refuse(f"capsule {want!r} is not in this book")
        chosen.append(hit)
    Path(out_path).write_text(json.dumps(chosen, sort_keys=True))
    return _out(ok=True, step="05", pushed=[c["capsule_id"] for c in chosen], file=out_path)


@server.tool()
def loop_deal_reconcile(peer_file: str, ledger: str | None = None) -> str:
    """Compare this node's requests with the halves the peer pushed in answer.

    A response pairs with the request it cites (``request_capsule_id``) and counts only
    when it verifies and its operator is the peer that request was addressed to. Per
    request: matched (same action digest), contradicted (different digest) or
    requester_only (nothing pushed for it). Absent is not failed.
    """
    try:
        theirs = _capsule_list(peer_file, "peer file")
    except ValueError as exc:
        return _refuse(str(exc))
    mine = [r for r in _capsules(ledger) if _compute(r).get("role") == "requester"]
    peer_of = {r["capsule_id"]: _compute(r).get("peer") for r in mine}
    theirs = [
        r for r in theirs
        if _compute(r).get("role") == "responder"
        and peer_of.get(_compute(r).get("request_capsule_id")) == _party(r)
        and verify_capsule(r).ok
    ]
    results = reconcile(
        mine, theirs,
        key_fn=lambda r: r["capsule_id"] if _compute(r).get("role") == "requester" else str(_compute(r).get("request_capsule_id")),
        digest_field="action_digest",
    )
    return _out(ok=True, step="05", fold=fold(results), exchanges=[
        {"key": r.key, "state": r.state.value, "detail": r.detail} for r in results
    ])


# ---------------------------------------------------------------------------
# 06 Settle
# ---------------------------------------------------------------------------


@server.tool()
def loop_settle(
    leg: str,
    sealer_role: str,
    amount_value: str = "",
    asset_code: str = "",
    asset_scale: int = 0,
    terms_ref: str = "",
    payment_ref: str = "",
    status: str = "",
    observed_at: str = "",
    delivery: str = "",
    prior: str = "",
    counterparty_capsule_id: str = "",
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str | None = None,
) -> str:
    """Seal one settlement leg (terms, payer_observed, payee_observed or delivered).

    *payment_ref* is a JSON object such as ``{"type": "x402.transaction", "value": "0x…",
    "network": "eip155:8453"}`` that the caller observed; this tool drives no rail and
    moves no money. The amount is an exact integer count of the smallest unit.
    """
    try:
        members: dict[str, Any] = {"terms_ref": terms_ref or None, "status": status or None, "observed_at": observed_at or None}
        if payment_ref:
            members["payment_ref"] = _json(payment_ref, "payment_ref")
        if delivery:
            members["delivery"] = _json(delivery, "delivery")
        if amount_value:
            amt = _settlement.amount(amount_value, asset_code, asset_scale)
            members["received" if leg == "payee_observed" else "amount"] = amt
        member = _settlement.build_leg(leg, sealer_role, **members)
        refs = [_settlement.counterparty_reference(counterparty_capsule_id)] if counterparty_capsule_id else None
        res = _settlement.seal_leg(
            member, operator=operator, developer=developer, prior=prior or None,
            references=refs, ledger=ledger,
        )
    except _settlement.SettlementError as exc:
        return _refuse(f"settlement refused: {exc}")
    except Exception as exc:
        return _refuse(f"settle failed: {exc}")
    return _out(ok=True, step="06", capsule_id=res.capsule_id, leg=leg, sealer_role=sealer_role)


@server.tool()
def loop_settle_verify(peer_file: str = "", ledger: str | None = None) -> str:
    """Derive settlement states from this node's legs and, if given, the peer's pushed ones.

    ``ok`` is the verifier's ``conforming``. No key policy is applied, so the report does
    not say whose keys signed the legs (``key_policy_applied`` in the report).
    """
    recs = [r for r in _capsules(ledger) if _settlement.SETTLEMENT_MEMBER in r]
    if peer_file:
        try:
            recs += _capsule_list(peer_file, "peer file")
        except ValueError as exc:
            return _refuse(str(exc))
    rep = _settlement.verify_settlements(recs)
    report = json.loads(json.dumps(dataclasses.asdict(rep), default=str))
    return _out(ok=bool(rep.conforming), step="06", legs=len(recs), report=report)


if __name__ == "__main__":
    server.run()
