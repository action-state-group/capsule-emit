#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check that the boundary-seal controls still behave as registered.

Read-only: every request is a GET of ``/v1/inclusion/<capsule_id>``, which
never registers anything. Never check a control with ``POST /v1/digest``:
that route registers the id it is given (see NEGATIVE_CONTROLS.md).

Controls:
  positive         the sealed capsule_id of task-boundary-seal-001 resolves
                   (200), to the registered entry hash and leaf.
  negative         ``"z" * 64`` (negative_control.json) is refused (400). It is
                   not hex, so the anchor's write path can never register it.
  fresh negative   a random 32-byte id drawn for this run is absent (404).

Exits 1, naming each control that changed, so a negative that starts passing
fails the check instead of going unnoticed. A request that gets no HTTP answer
(a timeout or a dropped connection) is a fault of the network or the anchor,
not a changed control: it is tried again, at most ATTEMPTS times in all, each
failure printed, and if none answers the check exits 2, naming the anchor as
unreachable. An HTTP answer, whatever its status, is never retried.

Usage: python check_controls.py [--anchor https://anchor.agentactioncapsule.org]
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATTEMPTS = 3
BACKOFF_SECONDS = (5, 15)


class Unreachable(Exception):
    """No attempt got an HTTP answer from the anchor."""


def get(url: str, sleep=time.sleep) -> tuple[int, dict]:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # urlopen returns once the status line and headers have arrived.
            resp = urllib.request.urlopen(url, timeout=30)
        except urllib.error.HTTPError as err:
            return err.code, {}
        except (urllib.error.URLError, OSError) as err:
            # No HTTP answer: URLError wraps DNS and connection failures;
            # TimeoutError and ConnectionResetError are OSErrors.
            print(f"attempt {attempt}/{ATTEMPTS}: no answer from {url}: {err}", file=sys.stderr)
            if attempt == ATTEMPTS:
                raise Unreachable(url) from err
            sleep(BACKOFF_SECONDS[attempt - 1])
            continue
        # The anchor answered: a failure reading the body is not retried.
        with resp:
            return resp.status, json.loads(resp.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--anchor", default="https://anchor.agentactioncapsule.org")
    anchor = parser.parse_args().anchor.rstrip("/")

    output = json.loads((HERE / "boundary_seal_output.json").read_text())
    negative = json.loads((HERE / "negative_control.json").read_text())
    positive_id = output["a2a_response_extension"]["capsule_id"]
    registered = output["anchor_receipt"]
    negative_id = negative["a2a_response_extension_tampered"]["capsule_id"]
    fresh_id = secrets.token_hex(32)

    failures = []

    status, body = get(f"{anchor}/v1/inclusion/{positive_id}")
    ok = (
        status == 200
        and body.get("entry_hash") == registered["entry_hash"]
        and body.get("leaf_index") == registered["leaf_index"]
    )
    print(f"positive        {positive_id}: HTTP {status} ({'ok' if ok else 'CHANGED'})")
    if not ok:
        failures.append("positive no longer resolves to its registered entry")

    status, _ = get(f"{anchor}/v1/inclusion/{negative_id}")
    ok = status == 400
    print(f"negative        {negative_id}: HTTP {status} ({'ok' if ok else 'CHANGED'})")
    if not ok:
        failures.append(f"negative control answered {status}, not 400")

    status, _ = get(f"{anchor}/v1/inclusion/{fresh_id}")
    ok = status == 404
    print(f"fresh negative  {fresh_id}: HTTP {status} ({'ok' if ok else 'CHANGED'})")
    if not ok:
        failures.append(f"a never-registered id answered {status}, not 404")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


def run() -> None:
    try:
        sys.exit(main())
    except Unreachable as err:
        print(f"FAIL: the anchor did not answer {err} after {ATTEMPTS} attempts", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    run()
