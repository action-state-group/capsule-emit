# SPDX-License-Identifier: Apache-2.0
"""capsule-emit CLI.

    capsule-emit ledger view <path>          — print the chain table
    capsule-emit ledger view <path> --json   — raw JSON array

Exit codes: 0 = ok, 1 = error.
"""
from __future__ import annotations

import argparse
import json
import sys


def _cmd_ledger_view(args: argparse.Namespace) -> int:
    from .ledger import read_ledger, view as _view

    if args.as_json:
        records = read_ledger(args.path)
        print(json.dumps(records, indent=2, default=str))
    else:
        _view(args.path)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capsule-emit",
        description="capsule-emit — emit + ledger CLI for Agent Action Capsules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ledger = sub.add_parser("ledger", help="ledger operations")
    ledger_sub = ledger.add_subparsers(dest="ledger_cmd", required=True)

    view = ledger_sub.add_parser("view", help="display the ledger as a chain table")
    view.add_argument("path", help="path to a JSONL ledger file")
    view.add_argument("--json", dest="as_json", action="store_true", help="raw JSON output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "ledger":
        if args.ledger_cmd == "view":
            return _cmd_ledger_view(args)

    parser.error(f"unknown command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
