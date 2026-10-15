# SPDX-License-Identifier: Apache-2.0
"""Language-neutral row access for the artifact store interlock.

Usage:
  python artifact_store_rows.py dump    <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py created <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py reset   mysql <host:port> <namespace>
  python artifact_store_rows.py copy    mysql <host:port> <from-namespace> <to-namespace>

``dump`` prints every row of one namespace in capsule_store_capsules and
capsule_store_artifacts, ordered by key, with BLOBs in hex, so the rows each
language writes can be compared byte for byte. The namespace column prints as
``<namespace>``, ``created_at`` only as present, and ``purged_at`` only as set
or NULL, since their values are wall-clock times. ``created`` prints each row's
exact ``created_at`` instead, to show a store left existing rows untouched.
``reset`` deletes a namespace (a fresh database without the tables is already
empty); ``copy`` duplicates one namespace's rows, timestamps included, into
another.
"""

from __future__ import annotations

import sys

TABLES = ("capsule_store_artifacts", "capsule_store_capsules")
MISSING_TABLE = 1146  # MySQL ER_NO_SUCH_TABLE


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
    action, kind, target, namespace, *rest = argv
    conn, mark = connect(kind, target)
    cursor = conn.cursor()
    if action == "reset":
        for table in TABLES:
            try:
                cursor.execute(f"DELETE FROM {table} WHERE namespace={mark}", (namespace,))
            except Exception as exc:
                if not exc.args or exc.args[0] != MISSING_TABLE:
                    raise
        conn.commit()
        return 0
    if action == "copy":
        (destination,) = rest
        for table in reversed(TABLES):  # parents first
            cursor.execute(f"SELECT * FROM {table} WHERE namespace={mark}", (namespace,))
            columns = [d[0] for d in cursor.description]
            rows = [tuple(destination if c == "namespace" else v for c, v in zip(columns, row))
                    for row in cursor.fetchall()]
            if rows:
                placeholders = ",".join([mark] * len(columns))
                cursor.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES({placeholders})", rows)
        conn.commit()
        return 0
    if action == "created":
        for table, key in (("capsule_store_capsules", "capsule_id"), ("capsule_store_artifacts", "capsule_id,name")):
            cursor.execute(
                f"SELECT {key},created_at FROM {table} WHERE namespace={mark} ORDER BY {key}", (namespace,)
            )
            for row in cursor.fetchall():
                print(table + "\t" + "\t".join(cell(c) for c in row))
        return 0
    if action != "dump":
        raise SystemExit(__doc__)
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
