# SPDX-License-Identifier: Apache-2.0
"""Language-neutral row dump for the artifact store interlock.

Prints every row of one namespace in capsule_store_capsules and
capsule_store_artifacts, ordered by key, with BLOBs in hex, so the rows each
language writes can be compared byte for byte. The namespace column prints
as ``<namespace>`` (MySQL writers use one namespace each), ``created_at``
prints only as present, and ``purged_at`` only as set or NULL, since the
values are wall-clock times.

Usage:
  python artifact_store_rows.py dump  <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py reset mysql <host:port> <namespace>
"""

from __future__ import annotations

import sys


def connect(kind: str, target: str):
    if kind == "sqlite":
        import sqlite3

        return sqlite3.connect(target), "?"
    import pymysql

    host, _, port = target.partition(":")
    return pymysql.connect(host=host, port=int(port), user="root", database="capsule_storage_test"), "%s"


def cell(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "x'" + bytes(value).hex() + "'"
    return repr(str(value))


def main(argv: list[str]) -> int:
    action, kind, target, namespace = argv
    conn, mark = connect(kind, target)
    cursor = conn.cursor()
    if action == "reset":
        for table in ("capsule_store_artifacts", "capsule_store_capsules"):
            cursor.execute(f"DELETE FROM {table} WHERE namespace={mark}", (namespace,))
        conn.commit()
        return 0
    cursor.execute(
        "SELECT capsule_id,capsule_bytes,producer_envelope,record_sha256,created_at IS NOT NULL "
        f"FROM capsule_store_capsules WHERE namespace={mark} ORDER BY capsule_id",
        (namespace,),
    )
    for row in cursor.fetchall():
        print("capsule\t<namespace>\t" + "\t".join(cell(c) for c in row))
    cursor.execute(
        "SELECT capsule_id,name,digest_field,content_bytes,content_sha256,retention_state,"
        "created_at IS NOT NULL,purged_at IS NOT NULL "
        f"FROM capsule_store_artifacts WHERE namespace={mark} ORDER BY capsule_id,name",
        (namespace,),
    )
    for row in cursor.fetchall():
        print("artifact\t<namespace>\t" + "\t".join(cell(c) for c in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
