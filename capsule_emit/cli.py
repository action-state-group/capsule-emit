# SPDX-License-Identifier: Apache-2.0
"""capsule-emit CLI.

Four rendering levels for the ledger:

    capsule-emit ledger view <path>              — L1: one-line-per-capsule table
    capsule-emit ledger view <path> --chains     — L2: chain tree grouped by parent
    capsule-emit ledger show <path> <capsule_id> — L3: full single-capsule detail
    capsule-emit ledger view <path> --json       — L4: raw JSON array

    capsule-emit verify --store <path>           — verify all capsules in a ledger

Exit codes: 0 = ok, 1 = error.
"""
from __future__ import annotations

import argparse
import json


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

    parser.error(f"unknown command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
