# SPDX-License-Identifier: Apache-2.0
"""The Python adapter of the cross-language artifact store interlock.

Runs one phase of test-vectors/artifact-store/interlock.json against one store
and prints one report line per step; every language's adapter must print the
same bytes (.github/scripts/artifact-store-interlock.sh).

    python artifact_interlock.py <phase> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>

target is a file path for jsonl and sqlite, and host:port/database for mysql.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import TextIO

from capsule_emit.artifact import ArtifactError, storage_checksum
from capsule_emit.artifact.record import decode_record, encode_record


def open_store(fmt: str, target: str, namespace: str, keys: list):
    if fmt == "jsonl":
        from capsule_emit.artifact.jsonl import JsonlArtifactStore

        return JsonlArtifactStore(target, namespace, keys), lambda: None
    if fmt == "sqlite":
        import sqlite3

        from capsule_emit.artifact.sqlite import SqliteArtifactStore

        conn = sqlite3.connect(target)
        conn.execute("PRAGMA foreign_keys=ON")
        return SqliteArtifactStore(conn, namespace, keys), conn.close
    if fmt == "mysql":
        import pymysql

        from capsule_emit.artifact.mysql import MysqlArtifactStore

        address, _, database = target.partition("/")
        host, _, port = address.partition(":")
        conn = pymysql.connect(host=host, port=int(port), user="root", database=database)
        return MysqlArtifactStore(conn, namespace, keys), conn.close
    raise SystemExit(f"unknown format {fmt}")


def outcome(call):
    try:
        return "ok", call()
    except ArtifactError as exc:
        return exc.code, None
    except Exception as exc:  # anything else is a storage failure, not a contract code
        print(f"storage error: {exc!r}", file=sys.stderr)
        return "storage_error", None


def run(args: list, out: TextIO) -> None:
    if len(args) != 5:
        raise SystemExit(__doc__)
    phase, fmt, target, namespace, directory = args
    plan = json.loads((Path(directory) / "interlock.json").read_text(encoding="utf-8"))
    vectors = json.loads((Path(directory) / "vectors.json").read_text(encoding="utf-8"))
    if phase not in plan["phases"]:
        raise SystemExit(f"unknown phase {phase}")
    trusted = plan["trusted"].get(phase, plan["trusted"]["default"])
    keys = [bytes.fromhex(vectors["keys"][name]) for name in trusted]
    store, close = open_store(fmt, target, namespace, keys)
    try:
        if phase == "write":
            store.init()
        for i, step in enumerate(plan["phases"][phase]):
            if "id" in step:
                capsule_id, subject = step["id"], json.dumps(step["id"])
            else:
                capsule_id = vectors["records"].get(step.get("record", ""), {}).get("capsule_id", "")
                subject = step.get("record", "")
            # Index steps name their paths or query in compact JSON.
            if step["op"] == "init_index":
                subject = json.dumps(step["paths"], separators=(",", ":"))
            elif step["op"] == "find":
                subject = json.dumps(step["where"], separators=(",", ":"), sort_keys=True)
            line = f"{i}\t{step['op']} {subject}\t"
            if step["op"] == "put":
                try:
                    record = decode_record(json.dumps(vectors["records"][step["record"]]))
                except ArtifactError:
                    line += "invalid"  # a record the codec cannot represent
                else:
                    line += outcome(lambda r=record: store.put(r))[0]
            elif step["op"] == "purge":
                line += outcome(lambda c=capsule_id: store.purge(c))[0]
            elif step["op"] == "init_index":
                line += outcome(lambda p=step["paths"]: store.init_index(p))[0]
            elif step["op"] == "find":
                # The interlock's finds fit one page; IDs print comma-separated.
                code, page = outcome(lambda w=step["where"]: store.find(where=w, limit=1000))
                line += code
                if page is not None and page.ids:
                    line += "\t" + ",".join(page.ids)
            elif step["op"] == "get":
                code, got = outcome(lambda c=capsule_id: store.get(c))
                line += code
                if got is not None:
                    ordered = dataclasses.replace(got, artifacts=sorted(got.artifacts, key=lambda a: a.name))
                    line += f"\t{storage_checksum(got)}\t{encode_record(ordered)}"
            else:
                raise SystemExit(f"unknown op {step['op']}")
            out.write(line + "\n")
    finally:
        close()


if __name__ == "__main__":
    run(sys.argv[1:], sys.stdout)
