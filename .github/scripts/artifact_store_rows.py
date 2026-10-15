# SPDX-License-Identifier: Apache-2.0
"""Language-neutral SQL access for the artifact store interlock.

Usage (target: an SQLite file, or host:port/database for MySQL):
  python artifact_store_rows.py dump     <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py created  <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py schema   <sqlite|mysql> <target>
  python artifact_store_rows.py copy     <sqlite|mysql> <target> <from-namespace> <to-namespace>
  python artifact_store_rows.py tamper   <sqlite|mysql> <target> <namespace>
  python artifact_store_rows.py createdb mysql <host:port/database>

``dump`` prints every row of one namespace in capsule_store_capsules and
capsule_store_artifacts, ordered by key, with BLOBs in hex, so the rows each
language writes can be compared byte for byte. The namespace column prints as
``<namespace>``. Timestamps are wall-clock values, so ``created_at`` and
``purged_at`` print only as NULL, or as whether the value has the contract's
UTC form (SQLite ``%Y-%m-%dT%H:%M:%fZ``; MySQL a DATETIME(6) within the last
hour of UTC_TIMESTAMP). ``created`` prints each row's exact ``created_at``, to
show a store left existing rows untouched. ``schema`` prints the tables' DDL as
the database reports it. ``copy`` duplicates a namespace's rows, timestamps
included; ``tamper`` changes one stored checksum (the interlock's negative
control); ``createdb`` drops and creates a MySQL database.
"""

from __future__ import annotations

import sys

TABLES = ("capsule_store_capsules", "capsule_store_artifacts")  # parents first
SQLITE_UTC = "____-__-__T__:__:__.___Z"


def connect(kind: str, target: str, database: bool = True):
    if kind == "sqlite":
        import sqlite3

        return sqlite3.connect(target), "?"
    import pymysql

    address, _, name = target.partition("/")
    host, _, port = address.partition(":")
    options = {"database": name} if database else {}
    return pymysql.connect(host=host, port=int(port), user="root", **options), "%s"


def cell(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "x'" + bytes(value).hex() + "'"
    return repr(str(value))


def utc(kind: str, column: str) -> str:
    if kind == "sqlite":
        return f"CASE WHEN {column} IS NULL THEN NULL ELSE {column} LIKE '{SQLITE_UTC}' END"
    return (
        f"CASE WHEN {column} IS NULL THEN NULL ELSE {column} BETWEEN "
        f"UTC_TIMESTAMP(6) - INTERVAL 1 HOUR AND UTC_TIMESTAMP(6) END"
    )


def main(argv: list[str]) -> int:
    action, kind, target, *rest = argv
    if action == "createdb":
        conn, _ = connect(kind, target, database=False)
        name = target.partition("/")[2]
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cursor.execute(f"CREATE DATABASE `{name}`")
        return 0
    conn, mark = connect(kind, target)
    cursor = conn.cursor()
    if action == "schema":
        for table in TABLES:
            if kind == "sqlite":
                cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            else:
                cursor.execute(f"SHOW CREATE TABLE {table}")
            for row in cursor.fetchall():
                print(row[-1])
        return 0
    namespace = rest[0]
    if action == "copy":
        destination = rest[1]
        for table in TABLES:
            cursor.execute(f"SELECT * FROM {table} WHERE namespace={mark}", (namespace,))
            columns = [d[0] for d in cursor.description]
            rows = [
                tuple(destination if c == "namespace" else v for c, v in zip(columns, row))
                for row in cursor.fetchall()
            ]
            if rows:
                values = ",".join([mark] * len(columns))
                cursor.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES({values})", rows)
        conn.commit()
        return 0
    if action == "tamper":
        cursor.execute(
            f"UPDATE capsule_store_capsules SET record_sha256={mark} WHERE namespace={mark}",
            ("0" * 64, namespace),
        )
        conn.commit()
        return 0
    if action == "created":
        for table, key in (("capsule_store_capsules", "capsule_id"), ("capsule_store_artifacts", "capsule_id,name")):
            cursor.execute(f"SELECT {key},created_at FROM {table} WHERE namespace={mark} ORDER BY {key}", (namespace,))
            for row in cursor.fetchall():
                print(table + "\t" + "\t".join(cell(c) for c in row))
        return 0
    if action != "dump":
        raise SystemExit(__doc__)
    cursor.execute(
        f"SELECT capsule_id,capsule_bytes,producer_envelope,record_sha256,{utc(kind, 'created_at')} "
        f"FROM capsule_store_capsules WHERE namespace={mark} ORDER BY capsule_id",
        (namespace,),
    )
    for row in cursor.fetchall():
        print("capsule\t<namespace>\t" + "\t".join(cell(c) for c in row))
    cursor.execute(
        "SELECT capsule_id,name,digest_field,content_bytes,content_sha256,retention_state,"
        f"{utc(kind, 'created_at')},{utc(kind, 'purged_at')} "
        f"FROM capsule_store_artifacts WHERE namespace={mark} ORDER BY capsule_id,name",
        (namespace,),
    )
    for row in cursor.fetchall():
        print("artifact\t<namespace>\t" + "\t".join(cell(c) for c in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
