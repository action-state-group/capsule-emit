# SPDX-License-Identifier: Apache-2.0
"""capsule-emit CLI.

Four rendering levels for the ledger:

    capsule-emit ledger view <path>              — L1: one-line-per-capsule table
    capsule-emit ledger view <path> --chains     — L2: chain tree grouped by parent
    capsule-emit ledger show <path> <capsule_id> — L3: full single-capsule detail
    capsule-emit ledger view <path> --json       — L4: raw JSON array

    capsule-emit verify --store <path>           — verify all capsules in a ledger

    capsule-emit permalink <capsule.json ...>    — build a demo verify-surface
                                                    permalink (withheld/bundle,
                                                    or single-capsule disclosed
                                                    via --reveal FIELD=payload.json)

Exit codes: 0 = ok, 1 = error.
"""
from __future__ import annotations

import argparse
import json
import sys


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

    # permalink
    from .permalink import DEFAULT_BASE_URL

    permalink_p = sub.add_parser(
        "permalink",
        help="build a demo verify-surface permalink (withheld/bundle only)",
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
        help="run verify() on every capsule first (no network); "
        "refuse to emit a URL if any capsule fails",
    )
    permalink_p.add_argument(
        "--with-statements",
        action="store_true",
        help="embed each capsule's signed_statement sidecar ({statement_b64, pubkey_pem}, "
        "read from <ledger_dir>/signed-statements/<capsule_id>.cose and the companion "
        "<capsule_id>.pub.pem if present) in the bundle. Default OFF: bundles ride in the "
        "URL fragment, and embedding statements can push a bundle well past practical URL "
        "length limits — measure before turning this on for a large bundle.",
    )
    permalink_p.add_argument(
        "--reveal",
        action="append",
        metavar="FIELD=payload.json",
        default=None,
        help="disclose a field (agent_input or agent_output) by reading its exact "
        "payload from a JSON file, e.g. --reveal agent_input=input.json. Wraps the "
        "capsule in the Disclosure Envelope shape the viewer reads. Single-capsule "
        "only (no --bundle, no --ledger/--from-run yielding more than one capsule) — "
        "the array-fragment bundle path doesn't support per-item disclosure (see "
        "capsule_emit/permalink.py module docstring for why).",
    )

    return parser


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


_REVEALABLE_FIELDS = ("agent_input", "agent_output")


def _parse_reveal_args(reveal: list[str]) -> dict:
    """Parse ``--reveal FIELD=path.json`` entries into {field: payload}."""
    from .permalink import PermalinkError, _load_json_file

    disclosures: dict = {}
    for entry in reveal:
        if "=" not in entry:
            raise PermalinkError(f"--reveal {entry!r}: expected FIELD=payload.json")
        field, _, path = entry.partition("=")
        if field not in _REVEALABLE_FIELDS:
            raise PermalinkError(
                f"--reveal {entry!r}: field must be one of {_REVEALABLE_FIELDS}"
            )
        disclosures[field] = _load_json_file(path)
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

    disclosures = None
    if args.reveal:
        if args.bundle or len(capsules) > 1:
            print(
                "permalink: --reveal requires exactly one capsule and no --bundle — "
                "the array-fragment bundle path doesn't support per-item disclosure "
                "(see capsule_emit/permalink.py module docstring for why).",
                file=sys.stderr,
            )
            return 2
        try:
            disclosures = _parse_reveal_args(args.reveal)
        except PermalinkError as exc:
            print(f"permalink: {exc}", file=sys.stderr)
            return 1
        mismatches = _check_reveal_digests(capsules[0], disclosures)
        if mismatches:
            print(
                "permalink --reveal: disclosed payload does not match the committed "
                "digest — refusing to emit a URL",
                file=sys.stderr,
            )
            for m in mismatches:
                print(f"  {m}", file=sys.stderr)
            return 1
        print(f"permalink --reveal: {len(disclosures)}/{len(disclosures)} disclosed field(s) digest-match VALID")

    if args.with_statements:
        from .permalink import embed_signed_statements

        capsules, matched = embed_signed_statements(
            capsules,
            capsule_files=args.capsule_files or None,
            ledger_path=args.ledger,
            from_run=args.from_run,
        )
        print(
            f"permalink --with-statements: embedded {matched}/{len(capsules)} "
            "signed_statement(s) (looked for <ledger_dir>/signed-statements/<capsule_id>.cose)"
        )

    bundle = args.bundle or len(capsules) > 1
    url = build_url(capsules, base_url=args.base_url, bundle=bundle, disclosures=disclosures)
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

    if args.command == "permalink":
        return _cmd_permalink(args)

    parser.error(f"unknown command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
