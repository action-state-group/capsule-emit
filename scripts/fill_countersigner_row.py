#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fill a countersigner row's placeholder ``key_ids`` and ``since`` in
``witnesses.json`` from the operator's published authority key.

A countersigner row is committed with two placeholders -- the key and the
start date exist only once the instance is live -- and
``tests/test_witness_directory.py::test_committed_directory_validates`` fails
on them by design. This script replaces exactly those two values and nothing
else::

    python scripts/fill_countersigner_row.py --since 2026-10-22 \\
        --list https://countersign.actionstate.ai/countersigners.json --list-sha256 "$LIST_SHA"

(``--dry-run`` previews; ``--pubkey-file`` / a local ``--list`` path avoid the
network.)

**Where the key comes from.** ``GET <endpoint>/anchor/authority-pubkey`` on a
capsule-anchor instance returns ``{"pubkey_hex": ..., "key_id": ...}``:
``pubkey_hex`` is the raw 32-byte Ed25519 public key as 64 lowercase hex, and
``key_id`` is the first 16 hex characters of ``sha256(pubkey)`` (the instance's
short id for STH / JSON signatures). A countersignature's ``signer.key_id`` is
the FULL 64-hex public key, so the directory row lists ``pubkey_hex``; the
short ``key_id`` is only used to cross-check that the response is
self-consistent. ``--pubkey-file`` reads a saved copy of that response body
(or a bare 64-hex key) instead of fetching.

**The pinned countersigner list must agree.** ``--list`` is the go-live
countersigner list -- the exact bytes stamps pin by SHA-256, a JSON array of
``{"name": ..., "key_ids": [...]}``, served by the instance as
``/countersigners.json`` -- and ``--list-sha256`` is the digest the operator
pinned for it. The script checks the
list's SHA-256 against the pin, finds the one row named ``Action State
Group``, and refuses to write unless that row lists exactly one key and it is
the ``pubkey_hex`` about to be written. The directory and the stamp's list
then name the same key.

**What it refuses.** A row that is already filled (either field), a key that
is not 64 lowercase hex or does not load as an Ed25519 public key, a
``key_id`` that does not match the key, a ``since`` that is not a calendar
date or is in the future, and any result that does not pass
``capsule_emit.witness_directory``. Nothing is written unless the whole file
validates after the fill.

``--mark-ready`` runs ``gh pr ready <PR>`` after a successful write; by
default the command is printed, not run.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capsule_emit.witness_directory import validate_directory  # noqa: E402

DEFAULT_ENDPOINT = "https://countersign.actionstate.ai"
PUBKEY_PATH = "/anchor/authority-pubkey"
DEFAULT_DIRECTORY = ROOT / "witnesses.json"
DEFAULT_PR = "226"
DEFAULT_REPO = "action-state-group/capsule-emit"
PLACEHOLDER = "PLACEHOLDER"

_PUBKEY_HEX = re.compile(r"^[0-9a-f]{64}$")
_SHORT_KEY_ID = re.compile(r"^[0-9a-f]{16}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIST_ROW_NAME = "Action State Group"


class FillError(Exception):
    """Anything that stops the fill. The message says what and where."""


# -- the key ------------------------------------------------------------------


def parse_pubkey_response(text: str) -> str:
    """Return the 64-hex Ed25519 public key from an ``/anchor/authority-pubkey``
    response body (or a bare 64-hex key), after checking it."""
    text = text.strip()
    key_id: str | None = None
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise FillError(f"authority-pubkey response is not JSON: {exc}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("pubkey_hex"), str):
            raise FillError('authority-pubkey response has no string "pubkey_hex"')
        pubkey_hex = body["pubkey_hex"]
        if "key_id" in body:
            key_id = body["key_id"]
            if not isinstance(key_id, str) or not _SHORT_KEY_ID.match(key_id):
                raise FillError(f"authority-pubkey key_id {key_id!r} is not 16 lowercase hex characters")
    else:
        pubkey_hex = text
    if not _PUBKEY_HEX.match(pubkey_hex):
        raise FillError(
            f"public key {pubkey_hex[:80]!r} is not a 32-byte Ed25519 key as 64 lowercase hex characters"
        )
    raw = bytes.fromhex(pubkey_hex)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise FillError(f"public key does not load as an Ed25519 public key: {exc}") from exc
    if key_id is not None and hashlib.sha256(raw).hexdigest()[:16] != key_id:
        raise FillError(
            f"authority-pubkey key_id {key_id} is not sha256(pubkey_hex)[:16] "
            f"({hashlib.sha256(raw).hexdigest()[:16]}): the response is not self-consistent"
        )
    return pubkey_hex


def _get(url: str, timeout: float = 15.0) -> bytes:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise FillError(f"{url!r} is not an https URL")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fill_countersigner_row"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- https checked above
            if resp.status != 200:
                raise FillError(f"GET {url}: HTTP {resp.status}")
            return resp.read()
    except OSError as exc:
        raise FillError(f"GET {url}: {exc}") from exc


def fetch_pubkey(endpoint: str, timeout: float = 15.0) -> tuple[str, str]:
    """GET ``<endpoint>/anchor/authority-pubkey``; return (url, body text)."""
    url = endpoint.rstrip("/") + PUBKEY_PATH
    return url, _get(url, timeout).decode("utf-8")


# -- the pinned countersigner list ---------------------------------------------


def read_list(source: str) -> bytes:
    """The list's exact bytes, from an https URL or a local path."""
    if "://" in source:
        return _get(source)
    return Path(source).expanduser().read_bytes()


def check_list(list_bytes: bytes, list_sha256: str, pubkey_hex: str) -> None:
    """Raise unless ``list_bytes`` hashes to the pin and its ``Action State
    Group`` row lists exactly ``pubkey_hex``."""
    pin = list_sha256.strip()
    if not _PUBKEY_HEX.match(pin):
        raise FillError(f"--list-sha256 {pin[:80]!r} is not 64 lowercase hex characters")
    actual = hashlib.sha256(list_bytes).hexdigest()
    if actual != pin:
        raise FillError(f"countersigner list SHA-256 is {actual}, not the pinned {pin}; refusing")
    try:
        rows = json.loads(list_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FillError(f"countersigner list is not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise FillError("countersigner list is not a JSON array")
    ours = [r for r in rows if isinstance(r, dict) and r.get("name") == LIST_ROW_NAME]
    if len(ours) != 1:
        raise FillError(f"countersigner list has {len(ours)} rows named {LIST_ROW_NAME!r}, expected exactly one")
    key_ids = ours[0].get("key_ids")
    if not isinstance(key_ids, list) or not key_ids:
        raise FillError(f"countersigner list row {LIST_ROW_NAME!r} has no key_ids")
    if key_ids[0] != pubkey_hex:
        raise FillError(
            f"countersigner list row {LIST_ROW_NAME!r} key_ids[0] is {key_ids[0]!r}, "
            f"not the authority key {pubkey_hex}; refusing"
        )
    if len(key_ids) != 1:
        raise FillError(
            f"countersigner list row {LIST_ROW_NAME!r} lists {len(key_ids)} keys; the extra one(s) "
            f"{key_ids[1:]} are not in the directory row; refusing"
        )


# -- the date -----------------------------------------------------------------


def check_since(since: str, today: datetime.date | None = None) -> str:
    if not _DATE.match(since):
        raise FillError(f"--since {since!r} is not YYYY-MM-DD")
    try:
        day = datetime.date.fromisoformat(since)
    except ValueError as exc:
        raise FillError(f"--since {since!r} is not a calendar date") from exc
    if day > (today or datetime.date.today()):
        raise FillError(f"--since {since} is in the future; use the date of the first live countersignature")
    return since


# -- the rewrite --------------------------------------------------------------


def _find_row(doc: dict, endpoint: str) -> tuple[int, dict]:
    rows = doc.get("countersigners")
    if not isinstance(rows, list):
        raise FillError("directory has no countersigners[] array")
    want = endpoint.rstrip("/")
    hits = [(i, r) for i, r in enumerate(rows) if isinstance(r, dict) and str(r.get("endpoint", "")).rstrip("/") == want]
    if len(hits) != 1:
        raise FillError(f"expected exactly one countersigners[] row with endpoint {endpoint}, found {len(hits)}")
    return hits[0]


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER)


def fill_text(text: str, endpoint: str, pubkey_hex: str, since: str) -> str:
    """Return ``text`` with the row's two placeholders replaced, or raise.

    The rewrite is textual (formatting, order and every other byte are kept);
    the result is then parsed and compared with the expected document, so a
    replacement that touched anything else is refused."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise FillError(f"directory is not JSON: {exc}") from exc
    i, row = _find_row(doc, endpoint)
    where = f"countersigners[{i}]"
    key_ids, old_since = row.get("key_ids"), row.get("since")
    key_is_placeholder = isinstance(key_ids, list) and len(key_ids) == 1 and _is_placeholder(key_ids[0])
    since_is_placeholder = _is_placeholder(old_since)
    if not key_is_placeholder and not since_is_placeholder:
        raise FillError(f"{where} ({endpoint}) is already filled: key_ids={key_ids} since={old_since!r}; refusing")
    if key_is_placeholder != since_is_placeholder:
        raise FillError(
            f"{where} ({endpoint}) is half filled (key_ids={key_ids}, since={old_since!r}); "
            "fix it by hand, refusing to guess"
        )

    out = text
    for old, new in ((key_ids[0], pubkey_hex), (old_since, since)):
        token = json.dumps(old)
        if text.count(token) != 1:
            raise FillError(f"placeholder {token} appears {text.count(token)} times in the file, expected once")
        out = out.replace(token, json.dumps(new), 1)

    expected = json.loads(text)
    expected["countersigners"][i]["key_ids"] = [pubkey_hex]
    expected["countersigners"][i]["since"] = since
    if json.loads(out) != expected:
        raise FillError("the rewrite changed something other than the two fields; refusing")
    errors = validate_directory(expected)
    if errors:
        raise FillError("directory does not validate after the fill:\n  " + "\n  ".join(errors))
    return out


def _write_atomically(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# -- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD: date of the first live countersignature")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"the row to fill, by endpoint (default {DEFAULT_ENDPOINT})")
    ap.add_argument("--pubkey-file", help="saved /anchor/authority-pubkey response body (or bare 64-hex key); no network")
    ap.add_argument(
        "--list",
        required=True,
        help="the pinned countersigner list: https URL (e.g. https://countersign.actionstate.ai/countersigners.json) "
        "or a local copy of the same bytes",
    )
    ap.add_argument("--list-sha256", required=True, help="the list's pinned SHA-256, 64 lowercase hex")
    ap.add_argument("--directory", default=str(DEFAULT_DIRECTORY), help="path to witnesses.json")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    ap.add_argument(
        "--mark-ready",
        action="store_true",
        help="run `gh pr ready` after a successful write (the PR flips before you push; prefer the printed command)",
    )
    ap.add_argument("--pr", default=DEFAULT_PR, help=f"PR number for --mark-ready (default {DEFAULT_PR})")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"repo for --mark-ready (default {DEFAULT_REPO})")
    args = ap.parse_args(argv)

    path = Path(args.directory)
    ready_cmd = ["gh", "pr", "ready", args.pr, "--repo", args.repo]
    try:
        since = check_since(args.since)
        if args.pubkey_file:
            source = args.pubkey_file
            body = Path(args.pubkey_file).read_text(encoding="utf-8")
        else:
            source, body = fetch_pubkey(args.endpoint)
        pubkey_hex = parse_pubkey_response(body)
        check_list(read_list(args.list), args.list_sha256, pubkey_hex)
        text = path.read_text(encoding="utf-8")
        new_text = fill_text(text, args.endpoint, pubkey_hex, since)
    except (FillError, OSError) as exc:
        print(f"fill_countersigner_row: ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"key   : {pubkey_hex}  (from {source})")
    print(f"since : {since}")
    print(f"list  : {args.list} (sha256 {args.list_sha256.strip()} ok; {LIST_ROW_NAME!r} lists this key only)")
    sys.stdout.writelines(
        difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), f"a/{path.name}", f"b/{path.name}")
    )
    if args.dry_run:
        print("dry run: nothing written; the filled file validates")
        return 0
    _write_atomically(path, new_text)
    print(f"{path}: filled and validated (python -m capsule_emit.witness_directory {path} -> ok)")
    if args.mark_ready:
        result = subprocess.run(ready_cmd, check=False)
        if result.returncode != 0:
            print(f"fill_countersigner_row: ERROR: {' '.join(ready_cmd)} exited {result.returncode}", file=sys.stderr)
            return 1
    else:
        print("next: commit + push the change, wait for CI green, then:")
        print("  " + " ".join(ready_cmd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
