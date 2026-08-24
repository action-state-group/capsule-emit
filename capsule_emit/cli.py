# SPDX-License-Identifier: Apache-2.0
"""capsule-emit CLI.

Four rendering levels for the ledger:

    capsule-emit ledger view <path>              — L1: one-line-per-capsule table
    capsule-emit ledger view <path> --chains     — L2: chain tree grouped by parent
    capsule-emit ledger show <path> <capsule_id> — L3: full single-capsule detail
    capsule-emit ledger view <path> --json       — L4: raw JSON array

    capsule-emit verify --store <path>           — verify all capsules in a ledger

    capsule-emit status <path> [--offline]       — ladder position: what's
                                                    logged, which checkpoint
                                                    covers what, which stamps
                                                    are back, honest lag;
                                                    read-only witness re-check
                                                    unless --offline

    capsule-emit permalink <capsule.json ...>    — build a demo verify-surface
                                                    permalink (withheld/bundle,
                                                    or disclosed via --reveal
                                                    FIELD=payload.json single-
                                                    capsule / --reveal
                                                    SELECTOR:FIELD=payload.json
                                                    per bundle item)

    capsule-emit evidence --ledger <path>        — build a Verification-stage
                                                    evidence comment (markdown)
                                                    from a ledger; fail-closed

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
    from .viewer import render_html, render_table

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
            from agent_action_capsule import verify_store

            verify_results = verify_store(records)
        except Exception:
            pass  # viewer degrades gracefully if verify unavailable

    if args.html:
        html_str = render_html(records, verify_results=verify_results, ledger_path=args.path)
        out_path = args.html
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(html_str)
        print(f"wrote {len(records)} record(s) → {out_path}")
        return 0

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
    view.add_argument(
        "--html",
        metavar="OUTPUT.html",
        default=None,
        help="write single-file static HTML ledger browse to OUTPUT.html",
    )

    # ledger show
    show = ledger_sub.add_parser("show", help="L3: full detail for one capsule")
    show.add_argument("path", help="path to a JSONL ledger file")
    show.add_argument("capsule_id", help="full or prefix (≥8 chars) capsule_id")

    # verify
    verify_p = sub.add_parser("verify", help="verify capsules")
    verify_p.add_argument("--store", dest="store_path", metavar="PATH", help="JSONL ledger to verify")

    # status
    status_p = sub.add_parser(
        "status",
        help="ladder position: what's logged, which checkpoint covers what, which "
        "stamps are back, honest witnessing lag -- contacts the witness read-only "
        "to re-confirm existing receipts unless --offline",
    )
    status_p.add_argument("path", help="path to a JSONL ledger file")
    status_p.add_argument(
        "--offline",
        action="store_true",
        help="skip the read-only witness re-check; report only what the ledger already holds",
    )
    status_p.add_argument("--json", dest="as_json", action="store_true", help="raw JSON output")

    # permalink
    from .permalink import DEFAULT_BASE_URL

    permalink_p = sub.add_parser(
        "permalink",
        help="build a demo verify-surface permalink (withheld/bundle, disclosed via --reveal)",
    )
    permalink_p.add_argument(
        "capsule_files",
        nargs="*",
        metavar="CAPSULE.json",
        help="one or more capsule JSON files (mutually exclusive with --ledger/--from-run)",
    )
    permalink_p.add_argument(
        "--ledger", metavar="PATH", default=None, help="read capsules from a JSONL ledger file"
    )
    permalink_p.add_argument(
        "--from-run",
        metavar="DIR",
        default=None,
        help="read capsules from a run directory (its ledger.jsonl if present, else its *.json files)",
    )
    permalink_p.add_argument(
        "--bundle",
        action="store_true",
        help="JSON-array fragment that renders the chain-navigation table; "
        "this is the DEFAULT whenever more than one capsule is supplied",
    )
    permalink_p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"verify-surface base URL (default: {DEFAULT_BASE_URL})",
    )
    permalink_p.add_argument(
        "--check",
        action="store_true",
        help="run verify() on every capsule first (no network); refuse to emit a URL if any capsule fails",
    )
    permalink_p.add_argument(
        "--reveal",
        action="append",
        metavar="FIELD=payload.json",
        default=None,
        help="disclose a field (agent_input or agent_output) by reading its exact "
        "payload from a JSON file, e.g. --reveal agent_input=input.json for a "
        "single capsule. For a bundle (--bundle, or --ledger/--from-run yielding "
        "more than one capsule), prefix with a selector: --reveal "
        "SELECTOR:FIELD=payload.json, where SELECTOR is a 1-based record number "
        "(as shown in the chain summary) or an >=8-char capsule_id prefix — repeat "
        "--reveal per field/item to disclose more than one. Wraps the targeted "
        "capsule(s) in the Disclosure Envelope shape the viewer reads; items with "
        "no --reveal stay withheld.",
    )

    # evidence
    evidence_p = sub.add_parser(
        "evidence",
        help="build a Verification-stage evidence comment (markdown) from a ledger; "
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
        default=DEFAULT_BASE_URL,
        help=f"verify-surface base URL for the viewer link (default: {DEFAULT_BASE_URL})",
    )
    evidence_p.add_argument(
        "--no-viewer-link",
        action="store_true",
        help="omit the verify-viewer permalink (offline verify commands remain)",
    )

    return parser


def _cmd_evidence(args: argparse.Namespace) -> int:
    from .evidence import EvidenceError, build_evidence_markdown
    from .permalink import PermalinkError, load_capsules

    try:
        capsules = load_capsules(
            capsule_files=args.capsule_files or None,
            ledger_path=args.ledger,
            from_run=args.from_run,
        )
    except PermalinkError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 1

    ledger_name = Path(args.ledger).name if args.ledger else "ledger.jsonl"
    try:
        markdown = build_evidence_markdown(
            capsules,
            issue_url=args.issue,
            title=args.title,
            base_url=args.base_url,
            viewer_link=not args.no_viewer_link,
            ledger_name=ledger_name,
        )
    except EvidenceError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(markdown)
        print(f"evidence: {len(capsules)}/{len(capsules)} capsule(s) VALID — wrote {args.out}")
    else:
        print(markdown)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from agent_action_capsule import verify_store

    from .ledger import read_ledger

    path = args.store_path
    records = read_ledger(path)
    if not records:
        print(f"verify: {path} — empty or not found")
        return 1
    results = verify_store(records)
    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    for r in results:
        status = "VALID" if r.ok else "INVALID"
        findings = [f"{f.check}: {f.detail}" for f in r.findings if f.severity == "error"]
        print(f"  {status}  {findings[0] if findings else ''}")
    print(f"\n{ok_count}/{len(results)} VALID" + (f"  — {fail_count} INVALID" if fail_count else ""))
    return 0 if fail_count == 0 else 1


def _cmd_status(args: argparse.Namespace) -> int:
    from .status import compute_status, render_status

    result = compute_status(args.path, offline=args.offline)

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    if result["capsule_count"] == 0 and result["checkpoint_count"] == 0:
        print(f"status: {args.path} — empty or not found")
        return 0

    render_status(result)
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


def _cmd_permalink(args: argparse.Namespace) -> int:
    from .permalink import PermalinkError, build_url, check_capsules, load_capsules, summarize

    try:
        capsules = load_capsules(
            capsule_files=args.capsule_files or None,
            ledger_path=args.ledger,
            from_run=args.from_run,
        )
    except PermalinkError as exc:
        print(f"permalink: {exc}", file=sys.stderr)
        return 1

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
            return 1
        print(f"permalink --check: {len(capsules)}/{len(capsules)} capsule(s) VALID")

    bundle = args.bundle or len(capsules) > 1

    disclosures = None
    if args.reveal:
        try:
            per_capsule = _parse_reveal_args(args.reveal, capsules)
        except PermalinkError as exc:
            print(f"permalink: {exc}", file=sys.stderr)
            return 1
        mismatches = _check_reveal_digests_per_capsule(capsules, per_capsule)
        if mismatches:
            print(
                "permalink --reveal: disclosed payload does not match the committed "
                "digest — refusing to emit a URL",
                file=sys.stderr,
            )
            for m in mismatches:
                print(f"  {m}", file=sys.stderr)
            return 1
        n_fields = sum(len(fields) for fields in per_capsule.values())
        print(
            f"permalink --reveal: {n_fields}/{n_fields} disclosed field(s) digest-match VALID "
            f"({len(per_capsule)}/{len(capsules)} capsule(s) disclosed)"
        )
        disclosures = per_capsule if bundle else next(iter(per_capsule.values()))

    url = build_url(capsules, base_url=args.base_url, bundle=bundle, disclosures=disclosures)
    frag_len = len(url.split("#", 1)[1].encode())
    if frag_len > _FRAGMENT_SIZE_WARN_BYTES:
        print(
            f"permalink: warning — URL fragment is {frag_len:,} bytes "
            f"(over the ~{_FRAGMENT_SIZE_WARN_BYTES // 1024}KB flag threshold); "
            "some browsers/proxies/shell history truncate or choke on URLs this long",
            file=sys.stderr,
        )
    print(summarize(capsules))
    print(url)
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

    parser.error(f"unknown command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
