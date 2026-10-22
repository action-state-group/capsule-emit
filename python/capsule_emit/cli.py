# SPDX-License-Identifier: Apache-2.0
"""capsule-emit CLI.

Four rendering levels for the ledger:

    capsule-emit ledger view <path>              — L1: one-line-per-capsule table
    capsule-emit ledger view <path> --chains     — L2: chain tree grouped by parent
    capsule-emit ledger show <path> <capsule_id> — L3: full single-capsule detail
    capsule-emit ledger view <path> --json       — L4: raw JSON array

    capsule-emit verify --store <path>           — verify all capsules in a ledger

    capsule-emit status <path> [--offline] [--verbose]
                                                 — receipts sealed, witnessed
                                                    and awaiting witness;
                                                    --verbose adds which
                                                    checkpoint covers what and
                                                    the stamp lag; read-only
                                                    witness re-check unless
                                                    --offline

    capsule-emit permalink --bundle FILE         — build a demo verify-surface
    capsule-emit permalink <capsule.json ...>      permalink: a bundle file
                                                    linked as it is, or capsule
                                                    files as a bundle with no
                                                    log proof (fields disclosed
                                                    via --reveal FIELD=payload.json
                                                    for one capsule / --reveal
                                                    SELECTOR:FIELD=payload.json
                                                    per item)

    capsule-emit evidence --ledger <path>        — build a Verification-stage
      [--bundle FILE]                              comment (markdown) from a
                                                    ledger; fail-closed. Its
                                                    viewer link carries FILE's
                                                    bundle, else one with no
                                                    log proof

    capsule-emit disclose <path> <id|range>      — bundle plus selected
      --audience NAME [--payloads all|selected]    payload content, a
      [--reveal SELECTOR:FIELD=payload.json ...]    completeness statement,
      [--suppress FIELD ...]                        and its own sealed
                                                      disclosure record —
                                                      the conscious sibling
                                                      of bundle; a
                                                      deliberate, recorded
                                                      act (never a default)

    capsule-emit bundle <ledger> -o FILE         — an Evidence Bundle for a
      (--exchange ID | --peer NODE |               third party, built by
       --since/--until TIME | --record ID |        agent-action-capsule's
       --all) [--interval-from SEQ]                producer from a ledger.jsonl
      [--closure-depth N] [--signing-key KEY.pem]  or a mesh ledger directory:
                                                    the records in scope, proven
                                                    under the signed checkpoint;
                                                    the whole log only with --all

Exit codes: 0 = ok, 1 = error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_ledger_view(args: argparse.Namespace) -> int:
    from .ledger import read_ledger
    from .ledger import view_chains as _view_chains
    from .viewer import render_table

    records = read_ledger(args.path)

    if args.as_json:
        print(json.dumps(records, indent=2, default=str))
        return 0

    if args.chains:
        _view_chains(args.path)
        return 0

    # Run verify for the verify column (fast — hash-only, no network)
    verify_results: list | None = None
    if records:
        try:
            from .signing import verify_store_signed

            verify_results = verify_store_signed(records)
        except Exception:
            pass  # viewer degrades gracefully if verify unavailable

    render_table(records, verify_results=verify_results, path=args.path)
    return 0


def _cmd_ledger_show(args: argparse.Namespace) -> int:
    from .ledger import show as _show

    found = _show(args.path, args.capsule_id)
    return 0 if found else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capsule-emit",
        description="capsule-emit — emit + ledger CLI for Agent Action Capsules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ledger = sub.add_parser("ledger", help="ledger operations")
    ledger_sub = ledger.add_subparsers(dest="ledger_cmd", required=True)

    # ledger view
    view = ledger_sub.add_parser("view", help="display the ledger (L1 table or L2 chain tree)")
    view.add_argument("path", help="path to a JSONL ledger file")
    view.add_argument(
        "--chains",
        action="store_true",
        help="L2: chain-tree view — groups capsules by parent (approved→executed→confirmed)",
    )
    view.add_argument("--json", dest="as_json", action="store_true", help="L4: raw JSON output")

    # ledger show
    show = ledger_sub.add_parser("show", help="L3: full detail for one capsule")
    show.add_argument("path", help="path to a JSONL ledger file")
    show.add_argument("capsule_id", help="full or prefix (≥8 chars) capsule_id")

    # verify
    verify_p = sub.add_parser("verify", help="verify capsules")
    verify_src = verify_p.add_mutually_exclusive_group(required=True)
    verify_src.add_argument("--store", dest="store_path", metavar="PATH", help="JSONL ledger to verify")
    verify_src.add_argument(
        "--bundle",
        dest="bundle_path",
        metavar="FILE.json",
        help="evidence file (evidence-bundle/v2) to verify offline: records, signatures, "
        "citation closure, the signed checkpoint, log coverage and per-record inclusion. "
        "Exit 0 VALID, 2 INCOMPLETE (nothing failed, something not shown), 1 INVALID",
    )
    verify_p.add_argument(
        "--json", dest="as_json", action="store_true", help="with --bundle: the full result as JSON"
    )
    verify_p.add_argument(
        "--witness-directory",
        metavar="WITNESSES.json",
        default=None,
        help="with --bundle: the witnesses (and keys) whose receipts to check "
        "(default: none, so no receipt is checked)",
    )
    verify_p.add_argument(
        "--require-signature",
        action="store_true",
        help="treat a record with no producer signature (producer_signature_unclaimed)  — for ledgers whose producer always signs."
        "as INVALID instead of a non-gating warning",
    )

    # status
    status_p = sub.add_parser(
        "status",
        help="receipts sealed, witnessed and awaiting witness (--verbose adds the "
        "checkpoint and stamp detail) -- contacts the witness read-only to "
        "re-check its existing signatures unless --offline",
    )
    status_p.add_argument("path", help="path to a JSONL ledger file")
    status_p.add_argument(
        "--offline",
        action="store_true",
        help="skip the read-only witness re-check; report only what the ledger already holds",
    )
    status_p.add_argument(
        "--witness-directory",
        metavar="WITNESSES.json",
        default=None,
        help="the witnesses (and keys) whose receipts to check for the grade "
        "(default: none, so no receipt is checked)",
    )
    status_p.add_argument(
        "--witness-url",
        dest="witness_url",
        action="append",
        metavar="URL",
        help="witness endpoint(s) to report backlog for (repeatable); defaults to "
        "CAPSULE_WITNESS_URL (no built-in default)",
    )
    status_p.add_argument("--json", dest="as_json", action="store_true", help="raw JSON output")
    status_p.add_argument(
        "--verbose",
        action="store_true",
        help="also show the checkpoint detail: checkpoint count, the latest checkpoint's "
        "grade and coverage, records awaiting checkpoint, checkpoints awaiting stamp, "
        "and the per-witness backlog",
    )

    # permalink
    from .permalink import MAX_INLINE_URL_BYTES

    permalink_p = sub.add_parser(
        "permalink",
        help="build a demo verify-surface permalink for an Evidence Bundle: a bundle file as it "
        "is (--bundle), or capsule files as an unproven bundle (disclosed fields via --reveal)",
        description="Build a verify-surface permalink whose fragment carries an Evidence Bundle "
        "(evidence-bundle/v2). --bundle FILE links that file's bundle unchanged, for example one "
        "`capsule-emit bundle` wrote with its log proof. Capsule files instead become a bundle with "
        "no log proof (agent-action-capsule's build_unproven_bundle), the last file as its root.",
    )
    permalink_p.add_argument(
        "capsule_files",
        nargs="*",
        metavar="CAPSULE.json",
        help="one or more capsule JSON files, in chain order; the last is the bundle's root "
        "(mutually exclusive with --bundle)",
    )
    permalink_p.add_argument(
        "--bundle",
        dest="bundle_file",
        metavar="FILE",
        default=None,
        help="link the Evidence Bundle in FILE as it is (mutually exclusive with capsule files, "
        "--reveal and --bundle-out)",
    )
    permalink_p.add_argument(
        "--base-url",
        required=True,
        help="verify-surface base URL (required; no default), for example "
        "https://verify.agentactioncapsule.org",
    )
    permalink_p.add_argument(
        "--check",
        action="store_true",
        help="verify first (no network) and refuse to emit a URL on a failure: every capsule "
        "file with verify(), or the --bundle file with agent-action-capsule's strict bundle verifier",
    )
    permalink_p.add_argument(
        "--reveal",
        action="append",
        metavar="FIELD=payload.json",
        default=None,
        help="disclose a field (agent_input or agent_output) by reading its exact "
        "payload from a JSON file, e.g. --reveal agent_input=input.json for a "
        "single capsule file. With more than one capsule file, prefix a selector: --reveal "
        "SELECTOR:FIELD=payload.json, where SELECTOR is a 1-based record number "
        "(as shown in the chain summary) or an >=8-char capsule_id prefix; repeat "
        "--reveal per field/item to disclose more than one. Goes in the Bundle's "
        "disclosures overlay; items with no --reveal stay withheld.",
    )
    permalink_p.add_argument(
        "--bundle-out",
        metavar="PATH",
        default=None,
        help="with capsule files: also write the exact Evidence Bundle the link carries (JCS JSON) "
        "to PATH, for hosting it behind a pointer permalink",
    )
    permalink_p.add_argument(
        "--bundle-location",
        action="append",
        metavar="URL",
        default=None,
        help="where the hosted bundle can be fetched (repeatable). Used only when the "
        f"inline permalink would exceed {MAX_INLINE_URL_BYTES // (1024 * 1024)} MiB: "
        "the fragment then carries a digest-checked pointer instead of the bundle. "
        "Without it an oversize permalink is refused.",
    )

    # bundle
    bundle_p = sub.add_parser(
        "bundle",
        help="write an Evidence Bundle (evidence-bundle/v2) of a ledger's records, proven against "
        "its signed checkpoint, for a third party: one exchange, one peer, one time window, one "
        "record, or --all",
        description="Write an Evidence Bundle (evidence-bundle/v2) built by agent-action-capsule's "
        "producer from a ledger.jsonl file or a mesh ledger directory. A scoped bundle carries "
        "exactly the records in scope, each with an inclusion proof under the signed checkpoint "
        "(no range proof unless --interval-from, no cited records unless --closure-depth); "
        "--all proves the whole log, seq 1 to the checkpoint tip. A record the checkpoint does "
        "not cover yet, or a range over a leaf that is not a record, is refused by name.",
    )
    bundle_p.add_argument(
        "ledger",
        metavar="LEDGER",
        help="a ledger.jsonl file, or a mesh ledger directory (capsules.jsonl, checkpoints.jsonl)",
    )
    bundle_p.add_argument("--exchange", metavar="ID", default=None, help="the records of one exchange")
    bundle_p.add_argument("--peer", metavar="NODE", default=None, help="the records naming one peer node")
    bundle_p.add_argument("--since", metavar="TIME", default=None, help="records at or after TIME (ISO 8601)")
    bundle_p.add_argument("--until", metavar="TIME", default=None, help="records at or before TIME (ISO 8601)")
    bundle_p.add_argument("--record", metavar="CAPSULE_ID", default=None, help="one record")
    bundle_p.add_argument(
        "--all",
        action="store_true",
        help="every record of the log up to its checkpoint, every counterparty, at citation "
        "closure depth 2, with a range proof from seq 1 when every leaf there is a record: only "
        "on purpose. A range cannot yet cover a leaf that is not a record (a padding line, which "
        "agent-action-capsule#339 will let a range cover; a ledger.jsonl's checkpoint stamps, which "
        "stay uncovered until a draft defines the stamp record), so with one the bundle carries "
        "no range, each record proven alone (INCOMPLETE), and says which leaf",
    )
    bundle_p.add_argument(
        "--interval-from",
        metavar="SEQ",
        type=int,
        default=None,
        help="with a scope: add a range proof from SEQ to the checkpoint tip, carrying every "
        "record in it (default: none, each record proven on its own)",
    )
    bundle_p.add_argument(
        "--closure-depth",
        metavar="N",
        type=int,
        default=None,
        help="with a scope: also carry the records the scoped records cite, N citation steps "
        "deep from the last one (default: 0)",
    )
    bundle_p.add_argument(
        "--signing-key",
        metavar="KEY.pem",
        default=None,
        help="mesh directory only: the node's Ed25519 key (PKCS#8 PEM) that signed the checkpoint, "
        "needed only when its checkpoint line carries no checkpoint_cose; never created",
    )
    bundle_p.add_argument("-o", "--out", metavar="PATH", required=True, help="the bundle file to write (JCS JSON)")

    # evidence
    evidence_p = sub.add_parser(
        "evidence",
        help="build a Verification-stage comment (markdown) from a ledger; "
        "fail-closed — every capsule is re-verified first",
    )
    evidence_p.add_argument(
        "capsule_files",
        nargs="*",
        metavar="CAPSULE.json",
        help="one or more capsule JSON files (mutually exclusive with --ledger/--from-run)",
    )
    evidence_p.add_argument(
        "--ledger", metavar="PATH", default=None, help="read capsules from a JSONL ledger file"
    )
    evidence_p.add_argument(
        "--from-run",
        metavar="DIR",
        default=None,
        help="read capsules from a run directory (its ledger.jsonl if present, else its *.json files)",
    )
    evidence_p.add_argument(
        "--issue",
        metavar="URL",
        default=None,
        help="the Ready issue this work implements (rendered as the Implements: line)",
    )
    evidence_p.add_argument(
        "--title", default="Verification evidence", help="comment heading (default: %(default)s)"
    )
    evidence_p.add_argument(
        "--out",
        metavar="FILE.md",
        default=None,
        help="write the markdown to FILE.md (default: stdout)",
    )
    evidence_p.add_argument(
        "--base-url",
        default=None,
        help="verify-surface base URL for the viewer link (required unless --no-viewer-link; "
        "no default), for example https://verify.agentactioncapsule.org",
    )
    evidence_p.add_argument(
        "--bundle",
        dest="bundle_file",
        metavar="FILE",
        default=None,
        help="the Evidence Bundle the viewer link carries, as it is (for example one "
        "`capsule-emit bundle` wrote, with its log proof); without it the link carries the "
        "capsules as a bundle with no log proof",
    )
    evidence_p.add_argument(
        "--no-viewer-link",
        action="store_true",
        help="omit the verify-viewer permalink (offline verify commands remain)",
    )

    # disclose
    disclose_p = sub.add_parser(
        "disclose",
        help="bundle plus selected payload content, a completeness statement, and its "
        "own sealed disclosure record — a deliberate, recorded act (bundle's conscious "
        "sibling); never a default",
    )
    disclose_p.add_argument("path", help="path to a JSONL ledger file")
    disclose_p.add_argument(
        "selector",
        metavar="id|range",
        help="a capsule_id (full or >=8-char prefix); an id1..id2 contiguous range "
        "(inclusive, ledger order — 'contiguous' completeness); or an explicit "
        "id1,id2,... list ('producer-selected' completeness)",
    )
    disclose_p.add_argument(
        "--audience", required=True, metavar="NAME", help="who this disclosure is for"
    )
    disclose_p.add_argument(
        "--payloads",
        choices=("all", "selected"),
        default="all",
        help="'all' (default) requires a --reveal payload for every eligible field not "
        "in --suppress on every selected record, refusing to under-disclose silently; "
        "'selected' discloses exactly the --reveal payloads supplied, nothing implied",
    )
    disclose_p.add_argument(
        "--reveal",
        action="append",
        metavar="SELECTOR:FIELD=payload.json",
        default=None,
        help="disclose a field (agent_input or agent_output) for one selected record — "
        "SELECTOR is a 1-based record number (over the full ledger) or an >=8-char "
        "capsule_id prefix; repeat --reveal per field/record",
    )
    disclose_p.add_argument(
        "--suppress",
        action="append",
        metavar="FIELD",
        default=None,
        help="withhold FIELD (agent_input or agent_output) from every selected record "
        "for this audience, recorded rather than silently dropped — repeatable",
    )
    disclose_p.add_argument("--json", dest="as_json", action="store_true", help="raw JSON output")
    disclose_p.add_argument(
        "--out", metavar="FILE.json", default=None, help="write the disclosure to FILE.json"
    )

    return parser


def _cmd_evidence(args: argparse.Namespace) -> int:
    from .permalink import PermalinkError, load_capsules
    from .verification_comment import VerificationCommentError, build_verification_comment

    try:
        capsules = load_capsules(
            capsule_files=args.capsule_files or None,
            ledger_path=args.ledger,
            from_run=args.from_run,
        )
    except PermalinkError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 1

    bundle = None
    if args.bundle_file:
        try:
            with open(args.bundle_file, encoding="utf-8") as fh:
                bundle = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"evidence: {args.bundle_file}: {exc}", file=sys.stderr)
            return 1

    ledger_name = Path(args.ledger).name if args.ledger else "ledger.jsonl"
    try:
        markdown = build_verification_comment(
            capsules,
            bundle=bundle,
            issue_url=args.issue,
            title=args.title,
            base_url=args.base_url,
            viewer_link=not args.no_viewer_link,
            ledger_name=ledger_name,
        )
    except VerificationCommentError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(markdown)
        print(f"evidence: {len(capsules)}/{len(capsules)} capsule(s) VALID — wrote {args.out}")
    else:
        print(markdown)
    return 0


def _witness_directory_arg(args: argparse.Namespace) -> dict | None:
    """``--witness-directory``, read and validated; ``None`` when not given.
    Raises ``ValueError`` naming every problem."""
    from .witness_directory import load_directory

    path = getattr(args, "witness_directory", None)
    return load_directory(path) if path else None


def _cmd_verify_bundle(args: argparse.Namespace) -> int:
    from .evidence_file import check_evidence_file, load_evidence_file

    try:
        bundle = load_evidence_file(args.bundle_path)
        directory = _witness_directory_arg(args)
    except (ValueError, OSError) as err:
        print(f"verify: {err}")
        return 1
    check = check_evidence_file(bundle, require_signature=args.require_signature, witness_directory=directory)
    if args.as_json:
        print(json.dumps(check.to_dict(), indent=2))
        return _VERDICT_EXIT[check.verdict]
    if not check.kind_ok:
        print(f"verify: {args.bundle_path} — not an evidence-bundle/v2 file")
        return 1
    marks = {"pass": "PASS     ", "withheld": "NOT SHOWN", "fail": "FAIL     "}
    for c in check.checks:
        print(f"  {marks.get(c.status, c.status)}  {c.plain}")
        for finding in c.findings:
            print(f"               {finding}")
    if check.witness is not None:
        print(f"  {marks.get(check.witness.status, check.witness.status)}  {check.witness.plain}")
        for receipt in check.witness_receipts:
            print(f"               {receipt['ts_url']}: {receipt['reason']}")
        for finding in check.witness.findings:
            if not finding.startswith(("witness_unverified:", "witness_receipt_invalid:")):
                print(f"               {finding}")
    if check.checkpoint_authenticated:
        signer = (
            "; its key signed every record"
            if check.signer_matches_checkpoint
            else "; some records name a different key"
        )
        print(
            f"\n{len(check.records)} record(s) in log {check.checkpoint.get('log_id')}, "
            f"under a checkpoint signed {check.checkpoint.get('timestamp')}{signer}"
        )
    elif any(c.name == "checkpoint" and c.status == "fail" for c in check.checks):
        print(f"\n{len(check.records)} record(s); the checkpoint does not match its signature: not tied to any log")
    else:
        print(f"\n{len(check.records)} record(s); no signed checkpoint: not tied to any committed log")
    if check.missing:
        print(f"{len(check.missing)} cited record(s) not in the file (declared by the file)")
    print(check.verdict)
    return _VERDICT_EXIT[check.verdict]


# VALID: everything proven. INCOMPLETE: nothing failed, something not shown.
_VERDICT_EXIT = {"VALID": 0, "INVALID": 1, "INCOMPLETE": 2}


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.bundle_path:
        return _cmd_verify_bundle(args)
    from .ledger import read_ledger
    from .signing import verify_store_signed

    path = args.store_path
    records = read_ledger(path)
    if not records:
        print(f"verify: {path} — empty or not found")
        return 1
    results = verify_store_signed(records, require_signature=args.require_signature)
    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    for r in results:
        status = "VALID" if r.ok else "INVALID"
        findings = [
            f"{f.detail}" if f.check is None else f"{f.check}: {f.detail}"
            for f in r.findings
            if f.severity == "error"
        ]
        print(f"  {status}  {findings[0] if findings else ''}")
    print(f"\n{ok_count}/{len(results)} VALID" + (f"  — {fail_count} INVALID" if fail_count else ""))
    unclaimed_count = sum(
        1
        for r in results
        for f in r.findings
        if f.code == "producer_signature_unclaimed" and f.severity == "warning"
    )
    if unclaimed_count:
        print(
            f"{unclaimed_count} record(s) carry no producer signature "
            "(producer_signature_unclaimed)"
        )
    return 0 if fail_count == 0 else 1


def _cmd_status(args: argparse.Namespace) -> int:
    from .status import compute_status, render_status

    try:
        directory = _witness_directory_arg(args)
    except (ValueError, OSError) as err:
        print(f"status: {err}", file=sys.stderr)
        return 1
    result = compute_status(
        args.path, offline=args.offline, ts_url=args.witness_url, witness_directory=directory
    )

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    if result["capsule_count"] == 0 and result["checkpoint_count"] == 0:
        print(f"status: {args.path} — empty or not found")
        return 0

    render_status(result, verbose=args.verbose)
    return 0


_REVEALABLE_FIELDS = ("agent_input", "agent_output")


def _resolve_capsule_by_selector(capsules: list[dict], selector: str) -> dict:
    """Resolve a ``--reveal`` SELECTOR (1-based record number, or an >=8-char
    capsule_id prefix — same prefix convention as ``ledger show``) to a capsule."""
    from .permalink import PermalinkError, _capsule_id_of

    if selector.isdigit() and len(selector) < 8:
        idx = int(selector)
        if not (1 <= idx <= len(capsules)):
            raise PermalinkError(
                f"--reveal: record number {idx} out of range (1-{len(capsules)})"
            )
        return capsules[idx - 1]
    if len(selector) < 8:
        raise PermalinkError(
            f"--reveal: selector {selector!r} must be a 1-based record number or "
            "an >=8-char capsule_id prefix"
        )
    matches = [c for c in capsules if _capsule_id_of(c).startswith(selector)]
    if not matches:
        raise PermalinkError(f"--reveal: no capsule matches capsule_id prefix {selector!r}")
    if len(matches) > 1:
        raise PermalinkError(
            f"--reveal: capsule_id prefix {selector!r} matches {len(matches)} capsules — "
            "use more characters"
        )
    return matches[0]


def _parse_reveal_args(reveal: list[str], capsules: list[dict]) -> dict[str, dict]:
    """Parse ``--reveal FIELD=path.json`` (exactly one capsule) or ``--reveal
    SELECTOR:FIELD=path.json`` (bundle) entries into
    ``{capsule_id: {field: payload}}``."""
    from .permalink import PermalinkError, _capsule_id_of, _load_json_file

    disclosures: dict[str, dict] = {}
    multi = len(capsules) > 1
    for entry in reveal:
        if "=" not in entry:
            raise PermalinkError(f"--reveal {entry!r}: expected FIELD=payload.json")
        key, _, path = entry.partition("=")
        if ":" in key:
            selector, _, field = key.partition(":")
            cap = _resolve_capsule_by_selector(capsules, selector)
        elif multi:
            raise PermalinkError(
                f"--reveal {entry!r}: more than one capsule — use "
                "SELECTOR:FIELD=payload.json (SELECTOR = 1-based record number "
                "or an >=8-char capsule_id prefix)"
            )
        else:
            field = key
            cap = capsules[0]
        if field not in _REVEALABLE_FIELDS:
            raise PermalinkError(
                f"--reveal {entry!r}: field must be one of {_REVEALABLE_FIELDS}"
            )
        disclosures.setdefault(_capsule_id_of(cap), {})[field] = _load_json_file(path)
    return disclosures


def _check_reveal_digests(capsule: dict, disclosures: dict) -> list[str]:
    """Recompute each disclosed field's digest and compare to the committed one.

    Returns a list of mismatch descriptions (empty = every disclosed field
    matches). A bad disclosure must never silently ship — this is the CLI's
    own local guard, independent of what the viewer later re-checks.
    """
    from .core import _digest

    ca = (capsule.get("model_attestation") or {}).get("compute_attestation") or {}
    mismatches = []
    for field, payload in disclosures.items():
        committed = ca.get(f"{field}_digest")
        if not committed:
            mismatches.append(f"{field}: capsule has no {field}_digest to disclose against")
            continue
        recomputed = _digest(payload)
        if recomputed != committed:
            mismatches.append(f"{field}: committed {committed[:12]}… != disclosed-payload {recomputed[:12]}…")
    return mismatches


def _check_reveal_digests_per_capsule(capsules: list[dict], disclosures: dict[str, dict]) -> list[str]:
    """Run ``_check_reveal_digests`` per disclosed capsule; mismatches are
    prefixed with the capsule_id so a bundle report identifies which item
    failed."""
    from .permalink import _capsule_id_of

    by_id = {_capsule_id_of(c): c for c in capsules}
    mismatches: list[str] = []
    for cid, fields in disclosures.items():
        cap = by_id[cid]
        mismatches.extend(f"{cid[:16]}…  {m}" for m in _check_reveal_digests(cap, fields))
    return mismatches


_FRAGMENT_SIZE_WARN_BYTES = 16 * 1024


def _permalink_bundle_file(args: argparse.Namespace) -> tuple[dict, str] | None:
    """``--bundle FILE``: the file's bundle, unchanged, and its summary line;
    ``None`` after printing why it cannot be linked."""
    from .permalink import summarize

    if args.capsule_files or args.reveal or args.bundle_out:
        print(
            "permalink: --bundle links a finished bundle as it is; it takes no capsule files, "
            "--reveal or --bundle-out",
            file=sys.stderr,
        )
        return None
    try:
        with open(args.bundle_file, encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"permalink: {args.bundle_file}: {exc}", file=sys.stderr)
        return None
    records = bundle.get("records") if isinstance(bundle, dict) else None
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        print(f"permalink: {args.bundle_file}: not an Evidence Bundle (no records)", file=sys.stderr)
        return None
    if args.check:
        from agent_action_capsule.bundle import verify_bundle

        verdict = verify_bundle(bundle, strict=True).verdict
        if verdict == "INVALID":
            print(
                f"permalink --check: {args.bundle_file} is INVALID under strict verification -- "
                "refusing to emit a URL (run `capsule-emit verify --bundle` for the findings)",
                file=sys.stderr,
            )
            return None
        print(f"permalink --check: {args.bundle_file} is {verdict} under strict verification")
    return bundle, summarize(records)


def _permalink_capsule_files(args: argparse.Namespace) -> tuple[dict, str] | None:
    """Capsule files: the unproven bundle they make, and its summary line;
    ``None`` after printing why there is none."""
    from agent_action_capsule.bundle import BundleBuildError, build_unproven_bundle
    from agent_action_capsule.canonical import jcs

    from .permalink import PermalinkError, _load_json_file, check_capsules, summarize

    if not args.capsule_files:
        print("permalink: no input -- pass capsule JSON file(s) or --bundle FILE", file=sys.stderr)
        return None
    try:
        capsules = [_load_json_file(p) for p in args.capsule_files]
    except (OSError, ValueError) as exc:
        print(f"permalink: {exc}", file=sys.stderr)
        return None

    if args.check:
        results = check_capsules(capsules)
        failures = [(c, r) for c, r in zip(capsules, results) if not r.ok]
        if failures:
            print(
                f"permalink --check: {len(failures)}/{len(capsules)} capsule(s) FAILED "
                "verify() — refusing to emit a URL",
                file=sys.stderr,
            )
            for cap, res in failures:
                cid = (cap.get("capsule_id") or "<no-capsule_id>")[:16]
                detail = "; ".join(f"{f.check}: {f.detail}" for f in res.errors) or "verification failed"
                print(f"  {cid}  {detail}", file=sys.stderr)
            return None
        print(f"permalink --check: {len(capsules)}/{len(capsules)} capsule(s) VALID")

    disclosures = None
    if args.reveal:
        try:
            disclosures = _parse_reveal_args(args.reveal, capsules)
        except PermalinkError as exc:
            print(f"permalink: {exc}", file=sys.stderr)
            return None
        mismatches = _check_reveal_digests_per_capsule(capsules, disclosures)
        if mismatches:
            print(
                "permalink --reveal: disclosed payload does not match the committed "
                "digest — refusing to emit a URL",
                file=sys.stderr,
            )
            for m in mismatches:
                print(f"  {m}", file=sys.stderr)
            return None
        n_fields = sum(len(fields) for fields in disclosures.values())
        print(
            f"permalink --reveal: {n_fields}/{n_fields} disclosed field(s) digest-match VALID "
            f"({len(disclosures)}/{len(capsules)} capsule(s) disclosed)"
        )

    try:
        # Closure deep enough to reach every supplied record from the root.
        bundle = build_unproven_bundle(
            capsules,
            root=capsules[-1].get("capsule_id"),
            closure_depth=len(capsules),
            disclosures=disclosures,
        )
    except BundleBuildError as exc:
        print(f"permalink: {exc}", file=sys.stderr)
        return None
    if args.bundle_out:
        Path(args.bundle_out).write_bytes(jcs(bundle))
    return bundle, summarize(capsules)


def _cmd_permalink(args: argparse.Namespace) -> int:
    from .permalink import PermalinkError, build_url

    built = _permalink_bundle_file(args) if args.bundle_file else _permalink_capsule_files(args)
    if built is None:
        return 1
    bundle, summary = built
    try:
        url = build_url(bundle, base_url=args.base_url, bundle_locations=args.bundle_location)
    except PermalinkError as exc:
        print(f"permalink: {exc}", file=sys.stderr)
        return 1
    frag_len = len(url.split("#", 1)[1].encode())
    if frag_len > _FRAGMENT_SIZE_WARN_BYTES:
        print(
            f"permalink: warning — URL fragment is {frag_len:,} bytes "
            f"(over the ~{_FRAGMENT_SIZE_WARN_BYTES // 1024}KB flag threshold); "
            "some browsers/proxies/shell history truncate or choke on URLs this long",
            file=sys.stderr,
        )
    print(summary)
    print(url)
    return 0


def _mesh_fields(capsule: dict) -> dict:
    """The exchange and peers a Mesh-LLM capsule names."""
    compute = (capsule.get("model_attestation") or {}).get("compute_attestation") or {}
    poc = compute.get("x-mesh-poc-v1") or {}
    serving = poc.get("serving_provenance") or {}
    settlement = compute.get("x-mesh-settlement-v1") or {}
    return {
        "exchange_id": serving.get("exchange_id") or settlement.get("exchange_id"),
        "peers": {p for p in (serving.get("served_by_node_id"), serving.get("requesting_party")) if p},
    }


def _in_scope(capsule: dict, args: argparse.Namespace) -> bool:
    if args.all:
        return True
    if args.record:
        return capsule.get("capsule_id") == args.record
    if args.exchange:
        return _mesh_fields(capsule)["exchange_id"] == args.exchange
    if args.peer:
        return args.peer in _mesh_fields(capsule)["peers"]
    stamp = capsule.get("timestamp") or ""
    return (not args.since or stamp >= args.since) and (not args.until or stamp <= args.until)


def _records_in_scope(
    log, args: argparse.Namespace, up_to_seq: int | None = None
) -> tuple[list, int, tuple[int, str, str] | None]:
    """The records of ``log`` in scope, in ledger order; how many in-scope
    records lie past ``up_to_seq`` and were left out; and the first leaf at or
    below ``up_to_seq`` that is not a record ``(seq, what, until)``, which no
    range can cover yet; ``until`` says what would let one. Without ``up_to_seq`` the records past the checkpoint are
    included (the producer refuses those by name)."""
    seen: set = set()
    selected: list = []
    left_out = 0
    non_record: tuple[int, str, str] | None = None
    after = 0
    while True:
        page = log.scan_entries(after, 1000)
        if not page:
            return selected, left_out, non_record
        after = page[-1].seq
        for entry in page:
            within = up_to_seq is not None and entry.seq <= up_to_seq
            capsule_id = entry.value.hex()
            if capsule_id in seen:
                if within and non_record is None:
                    # agent-action-capsule#339 leaves a repeated leaf out of scope.
                    non_record = (entry.seq, "repeats an earlier leaf", "a range over a repeated leaf stays refused")
                continue
            seen.add(capsule_id)
            try:
                record = log.get(capsule_id)
            except Exception as exc:  # the artifact-store contract's not-found code
                if getattr(exc, "code", None) == "not_found":
                    if within and non_record is None:
                        non_record = (
                            entry.seq,
                            "is a padding or checkpoint-stamp leaf, not a record",
                            "a range over padding waits for agent-action-capsule#339; over a checkpoint "
                            "stamp, for a draft that defines the stamp record",
                        )
                    continue
                raise
            capsule = json.loads(record.capsule)
            if not _in_scope(capsule, args):
                continue
            if up_to_seq is not None and entry.seq > up_to_seq:
                left_out += 1
            else:
                selected.append(capsule)


def _open_bundle_log(args: argparse.Namespace):
    from .evidence_bundle import MeshLedgerLog
    from .ledger import is_ledger_store
    from .legacy_ledger import LegacyLedger

    path = Path(args.ledger)
    if is_ledger_store(path):
        if args.signing_key is not None:
            raise ValueError("--signing-key applies to a mesh ledger directory; a ledger store carries its own COSE checkpoints")
        return LegacyLedger(path)
    if path.is_dir():
        if not (path / "checkpoints.jsonl").is_file():
            raise ValueError(f"{path}: not a mesh ledger directory (no checkpoints.jsonl)")
        return MeshLedgerLog(path, signing_key=args.signing_key)
    if not path.is_file():
        raise ValueError(f"{path}: no ledger there")
    if args.signing_key is not None:
        raise ValueError("--signing-key applies to a mesh ledger directory; a ledger.jsonl carries its own COSE checkpoints")
    return LegacyLedger(path)


class _PinnedLog:
    """A log whose checkpoint state is read once: record selection and the
    proofs then describe the same checkpoint even while the log is written."""

    def __init__(self, log) -> None:
        self._log = log
        self._state = log.load_cll()

    def load_cll(self):
        return self._state

    def scan_entries(self, after_seq: int, limit: int):
        return self._log.scan_entries(after_seq, limit)

    def get(self, capsule_id: str):
        return self._log.get(capsule_id)


def _cmd_bundle(args: argparse.Namespace) -> int:
    from agent_action_capsule.bundle import (
        BundleBuildError,
        build_evidence_bundle,
        proof_source_from_cll,
        verify_bundle,
    )
    from agent_action_capsule.canonical import jcs
    from cll.log import CllError

    from .artifact.record import ArtifactError

    scopes = [bool(args.exchange), bool(args.peer), bool(args.since or args.until), bool(args.record), args.all]
    if sum(scopes) != 1:
        print(
            "bundle: name exactly one scope: --exchange ID, --peer NODE, --since/--until TIME, "
            "--record ID, or --all (the whole log, every counterparty)",
            file=sys.stderr,
        )
        return 1
    if args.all and (args.interval_from is not None or args.closure_depth is not None):
        print(
            "bundle: --all proves the whole log (range from seq 1, closure depth 2); "
            "--interval-from and --closure-depth apply to a scoped bundle",
            file=sys.stderr,
        )
        return 1
    try:
        log = _PinnedLog(_open_bundle_log(args))
        # --all proves the log up to its checkpoint: records appended since
        # are left out (named below), not refused.
        checkpoint = log.load_cll().checkpoint if args.all else None
        selected, left_out, non_record = _records_in_scope(log, args, checkpoint.indexed_seq if checkpoint else None)
        if not selected:
            reason = "no record covered by the checkpoint yet" if left_out else "no record in scope"
            print(f"bundle: {reason}", file=sys.stderr)
            return 1
        ids = [capsule["capsule_id"] for capsule in selected]
        # --all keeps the range from seq 1 when every leaf up to the checkpoint
        # is a record. A range cannot yet cover any other leaf (padding waits
        # for agent-action-capsule#339; checkpoint stamps for a draft that
        # defines the stamp record), so without one each record is proven
        # alone and the bundle reads INCOMPLETE.
        interval_from = args.interval_from
        if args.all:
            interval_from = None if non_record else 1
        bundle = build_evidence_bundle(
            proof_source_from_cll(log, log),
            root=ids[-1],
            include=ids[:-1],
            interval_from=interval_from,
            closure_depth=2 if args.all else (args.closure_depth if args.closure_depth is not None else 0),
        )
    except (BundleBuildError, CllError, ArtifactError, ValueError, OSError) as exc:
        print(f"bundle: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_bytes(jcs(bundle))
    verdict = verify_bundle(bundle, strict=True).verdict
    print(f"bundle: {len(bundle['records'])} record(s), {verdict} under strict verification -- wrote {args.out}")
    if args.all and non_record:
        seq, what, until = non_record
        print(f"bundle: no range: seq {seq} {what}; each record is proven alone ({until})")
    if left_out:
        print(f"bundle: {left_out} record(s) past the checkpoint left out; bundle again after the next checkpoint")
    return 0


def _cmd_disclose(args: argparse.Namespace) -> int:
    from .disclose import DiscloseError
    from .disclose import disclose as _disclose_fn
    from .ledger import read_ledger
    from .permalink import PermalinkError

    capsules = read_ledger(args.path)
    if not capsules:
        print(f"disclose: {args.path} — empty or not found", file=sys.stderr)
        return 1

    reveal = None
    if args.reveal:
        try:
            reveal = _parse_reveal_args(args.reveal, capsules)
        except PermalinkError as exc:
            print(f"disclose: {exc}", file=sys.stderr)
            return 1
        mismatches = _check_reveal_digests_per_capsule(capsules, reveal)
        if mismatches:
            print(
                "disclose --reveal: disclosed payload does not match the committed "
                "digest — refusing to disclose",
                file=sys.stderr,
            )
            for m in mismatches:
                print(f"  {m}", file=sys.stderr)
            return 1

    try:
        d = _disclose_fn(
            args.path,
            args.selector,
            audience=args.audience,
            payloads=args.payloads,
            reveal=reveal,
            suppress=args.suppress,
        )
    except DiscloseError as exc:
        print(f"disclose: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(json.dumps(d.to_dict(), indent=2, default=str))

    if args.as_json:
        print(json.dumps(d.to_dict(), indent=2, default=str))
        return 0

    n_fields = sum(len(env.get("disclosures") or {}) for env in d.envelopes.values())
    print(
        f"disclose: {len(d.record_ids)} record(s) → audience={d.audience!r}  "
        f"{n_fields} payload field(s) disclosed"
    )
    print(f"  records:  {d.completeness['records_note']}")
    print(f"  payloads: {d.completeness['payloads_note']}")
    print(f"  disclosure record: {d.disclosure_record['capsule_id']}")
    if args.out:
        print(f"  wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "ledger":
        if args.ledger_cmd == "view":
            return _cmd_ledger_view(args)
        if args.ledger_cmd == "show":
            return _cmd_ledger_show(args)

    if args.command == "verify":
        return _cmd_verify(args)

    if args.command == "status":
        return _cmd_status(args)

    if args.command == "permalink":
        return _cmd_permalink(args)

    if args.command == "evidence":
        return _cmd_evidence(args)

    if args.command == "disclose":
        return _cmd_disclose(args)

    if args.command == "bundle":
        return _cmd_bundle(args)

    parser.error(f"unknown command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
