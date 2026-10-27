# SPDX-License-Identifier: Apache-2.0
"""The artifact store's index extension: the shared helpers and the backend
paths the conformance vectors do not reach."""

from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

from capsule_emit.artifact import ConflictError, CorruptError, InvalidError, Store, conformance
from capsule_emit.artifact.index import (
    MAX_INDEX_PATHS,
    IndexStore,
    MemoryIndex,
    Page,
    check_query,
    decode_paths,
    encode_paths,
    extract,
    normalize_paths,
    parse_timestamp,
    value_key,
)
from capsule_emit.artifact.jsonl import JsonlArtifactStore
from capsule_emit.artifact.memory import MemoryArtifactStore
from capsule_emit.artifact.mysql import MysqlArtifactStore
from capsule_emit.artifact.sqlite import SCHEMA, SqliteArtifactStore

VECTORS = conformance.load_vectors()
TRUSTED = bytes.fromhex(VECTORS["keys"]["trusted"])
MYSQL = os.environ.get("CAPSULE_STORAGE_TEST_MYSQL", "")


def record(name):
    return conformance._record(VECTORS, name)


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T00:00:00.000001Z", 1),
        ("1970-01-01T00:00:00.0000019Z", 1),
        ("1970-01-01T00:00:00.5Z", 500_000),
        ("1970-01-01T01:00:00+01:00", 0),
        ("1969-12-31T23:00:00-01:00", 0),
        ("1969-12-31T23:59:59.999999Z", -1),
        ("2024-02-29T00:00:00Z", 1709164800_000000),
        ("2000-02-29T00:00:00Z", 951782400_000000),
        ("0000-01-01T00:00:00Z", -62167219200_000000),
        ("9999-12-31T23:59:59.999999+00:00", 253402300799_999999),
        ("2026-01-01T00:00:00+23:59", 1767225600_000000 - (23 * 60 + 59) * 60_000_000),
    ],
)
def test_parse_timestamp(value, want):
    assert parse_timestamp(value) == want


@pytest.mark.parametrize(
    "value",
    [
        "",
        "2023-02-29T00:00:00Z",
        "1900-02-29T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-00-01T00:00:00Z",
        "2026-01-00T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:60:00Z",
        "2026-01-01T00:00:00+00:60",
        "2026-01-01T00:00:00+24:00",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00z",
        "2026-01-01T00:00:00Z\n",
        "2026-01-01T00:00:00.1234567890Z",
        "2026-01-01T00:00:00+0000",
        "２０２６-01-01T00:00:00Z",
        "2026-01-01T00:00:0٠Z",
    ],
)
def test_parse_timestamp_rejects(value):
    assert parse_timestamp(value) is None


def test_extract_takes_strings_and_string_array_elements_only():
    got = extract(
        b'{"a":{"b":"x","c":["y",1,"y","z",null,{"k":"v"}],"d":{"e":1},"f":true,"g":null,"h":1.5},'
        b'"timestamp":"bad"}',
        ["a.b", "a.c", "a.d", "a.f", "a.g", "a.h", "a.b.x", "missing", "a.missing.deeper"],
    )
    assert got.ts is None
    assert got.entries == [("a.b", "x"), ("a.c", "y"), ("a.c", "z")]


def test_extract_reads_the_top_level_timestamp_only():
    assert extract(b'{"timestamp":"1970-01-01T00:00:01Z"}', []).ts == 1_000_000
    assert extract(b'{"timestamp":1}', []).ts is None
    assert extract(b'{"x":{"timestamp":"1970-01-01T00:00:01Z"}}', []).ts is None


def test_extract_of_non_object_and_undecodable_capsules():
    assert extract(b'["not an object"]', ["a"]).entries == []
    assert extract(b'"timestamp"', ["timestamp"]).entries == []
    for damaged in (b"{", b'{"a":1,"a":2}', b"\xff", b'{"a":NaN}'):
        with pytest.raises(CorruptError):
            extract(damaged, ["a"])


def test_paths_round_trip_and_validation():
    paths = normalize_paths(["b", "a.c"])
    assert encode_paths(paths) == '["a.c","b"]'
    assert decode_paths(encode_paths(paths)) == paths
    assert decode_paths(b'["a.c","b"]') == paths
    assert encode_paths(normalize_paths([])) == "[]"
    assert normalize_paths(iter(["x"])) == ["x"]
    assert len(normalize_paths([f"p{i}" for i in range(MAX_INDEX_PATHS)])) == MAX_INDEX_PATHS
    long_path = ".".join(["s" * 31] * 8)  # 255 bytes
    assert normalize_paths([long_path]) == [long_path]
    for bad in (["a", "a"], [""], ["a."], ["a b"], [1], ["s" * 32 + "." + "s" * 223], ["a\n"], "abc", None):
        with pytest.raises(InvalidError):
            normalize_paths(bad)


@pytest.mark.parametrize("stored", ["{", '["a","a"]', '[" "]', "null", '{"a":1}', "[1]", '["a"] x', 7])
def test_damaged_stored_declaration_is_corrupt(stored):
    with pytest.raises(CorruptError):
        decode_paths(stored)


def test_value_key():
    assert value_key("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert value_key("é") == hashlib.sha256(b"\xc3\xa9").hexdigest()


def test_check_query():
    c = check_query({"b": "2", "a": "1"}, "", "", 1, ["a", "b"])
    assert (c.where, c.since, c.after, c.limit) == ([("a", "1"), ("b", "2")], None, 0, 1)
    assert check_query(None, None, "9223372036854775807", 1000, []).after == (1 << 63) - 1
    for where, since, after, limit in [
        (None, None, None, 0),
        (None, None, None, True),
        (None, None, None, 1.0),
        (None, None, None, "1"),
        ({"a": 1}, None, None, 1),
        ({1: "a"}, None, None, 1),
        ({"undeclared": "x"}, None, None, 1),
        ([("a", "1")], None, None, 1),
        (None, 0, None, 1),
        (None, "2026-01-01", None, 1),
        (None, None, "+5", 1),
        (None, None, " 5", 1),
        (None, None, "5\n", 1),
        (None, None, "٥", 1),
        (None, None, "9223372036854775808", 1),
        (None, None, 5, 1),
    ]:
        with pytest.raises(InvalidError):
            check_query(where, since, after, limit, ["a", "b"])


def test_memory_index_before_and_after_declaration():
    index = MemoryIndex()
    index.add("id", b"{")  # ignored before a declaration
    assert index.declared is None
    with pytest.raises(InvalidError):
        index.find(None, None, None, 1)
    with pytest.raises(InvalidError):
        index.declare(["a", "a"])
    assert index.declare(["a"]) is True
    assert index.declare(["a"]) is False
    with pytest.raises(ConflictError):
        index.declare(["b"])
    with pytest.raises(CorruptError):
        index.add("id", b"{")
    assert index.declared == ["a"]
    index.add("one", b'{"a":"x"}')
    index.add("one", b'{"a":"y"}')  # already indexed: left where it is
    index.add("two", b'{"a":["x","y"]}')
    assert index.find({"a": "y"}, None, None, 10) == Page(ids=["two"])
    assert index.find({"a": "x"}, None, None, 1) == Page(ids=["one"], next="1")
    assert index.find({"a": "x"}, None, "1", 1) == Page(ids=["two"])
    assert index.find(None, None, "99", 1) == Page(ids=[])


def test_protocols():
    assert isinstance(MemoryArtifactStore("ns", [TRUSTED]), IndexStore)
    assert isinstance(SqliteArtifactStore(sqlite3.connect(":memory:"), "ns", [TRUSTED]), IndexStore)
    assert isinstance(MysqlArtifactStore(object(), "ns", [TRUSTED]), IndexStore)

    class Plain:
        namespace = "ns"

        def init(self): ...
        def put(self, record): ...
        def get(self, capsule_id): ...
        def purge(self, capsule_id): ...

    assert isinstance(Plain(), Store) and not isinstance(Plain(), IndexStore)


def test_jsonl_protocol(tmp_path):
    assert isinstance(JsonlArtifactStore(tmp_path / "a.jsonl", "ns", [TRUSTED]), IndexStore)


class StoreWithoutIndex:
    """A harness store that implements only the base contract."""

    def __init__(self, namespace, keys):
        self._inner = MemoryArtifactStore(namespace, keys)
        self.namespace = namespace

    def init(self):
        self._inner.init()

    def put(self, record):
        self._inner.put(record)

    def get(self, capsule_id):
        return self._inner.get(capsule_id)

    def purge(self, capsule_id):
        self._inner.purge(capsule_id)


def test_harness_without_the_extension_skips_index_cases():
    harness = conformance.Harness(new_backing=lambda: None, open=lambda _b, ns, keys: StoreWithoutIndex(ns, keys))
    outcomes = conformance.run(harness)
    assert [o for o in outcomes if o.status == "fail"] == []
    index_cases = [o for o in outcomes if o.name.startswith("index_")]
    assert index_cases and all(o.status == "skip" and "index" in o.detail for o in index_cases)


# ---- Backends ----------------------------------------------------------------


def old_schema_sqlite(path):
    """A database provisioned before the index tables existed."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA.split(";"):
        if statement.strip() and "capsule_store_index" not in statement:
            connection.execute(statement)
    return connection


def test_sqlite_index_on_older_schema(tmp_path):
    connection = old_schema_sqlite(tmp_path / "a.db")
    store = SqliteArtifactStore(connection, "older", [TRUSTED])
    store.put(record("index.a"))
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {t for t in tables if t.startswith("capsule_store_index")}
    with pytest.raises(InvalidError):
        store.find()
    store.init_index(["operator"])
    assert store.find(where={"operator": "op-a"}) == Page(ids=[record("index.a").capsule_id])
    store.put(record("index.c"))  # the same store now sees the tables
    assert store.find(where={"operator": "op-a"}).ids == [record("index.a").capsule_id, record("index.c").capsule_id]


def test_sqlite_index_damaged_rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "a.db", isolation_level=None)
    store = SqliteArtifactStore(connection, "damaged", [TRUSTED])
    store.init()
    store.put(record("index.a"))
    store.put(record("index.b"))
    connection.execute(
        "UPDATE capsule_store_capsules SET capsule_bytes=X'7B' WHERE namespace='damaged' AND capsule_id=?",
        (record("index.a").capsule_id,),
    )
    store.init_index(["operator"])
    assert store.find() == Page(ids=[record("index.b").capsule_id])

    connection.execute("UPDATE capsule_store_index_paths SET paths='{' WHERE namespace='damaged'")
    with pytest.raises(CorruptError):
        store.find()
    with pytest.raises(CorruptError):
        store.put(record("index.c"))  # fails closed and rolls back
    assert not connection.in_transaction
    with pytest.raises(CorruptError):
        store.init_index(["operator"])
    store.reindex(["developer"])  # replaces a damaged declaration
    assert store.find(where={"developer": "dev-1"}) == Page(ids=[record("index.b").capsule_id])
    with pytest.raises(InvalidError):
        store.init_index(["a", "a"])
    with pytest.raises(InvalidError):
        store.reindex(["a", "a"])


def test_sqlite_put_tx_rollback_leaves_no_index_rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "a.db", isolation_level=None)
    store = SqliteArtifactStore(connection, "ns", [TRUSTED])
    store.init_index(["operator"])
    connection.execute("BEGIN")
    store.put_tx(connection, record("index.a"))
    connection.execute("ROLLBACK")
    assert store.find() == Page(ids=[])
    store.put(record("index.a"))
    store.put(record("index.a"))  # a retry changes nothing
    assert connection.execute("SELECT COUNT(*) FROM capsule_store_index_records").fetchone() == (1,)


def test_jsonl_index_skips_damaged_line(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    damaged = '{"capsule_id":"' + "ab" * 32 + '","capsule":1}\n'
    path.write_text(damaged, encoding="utf-8")
    store = JsonlArtifactStore(path, "ns", [TRUSTED])
    store.put(record("index.a"))
    store.init_index(["operator"])
    assert store.find() == Page(ids=[record("index.a").capsule_id])
    with pytest.raises(InvalidError):
        store.reindex(["a", "a"])
    store.reindex(["developer"])
    assert store.find(where={"developer": "dev-1"}) == Page(ids=[record("index.a").capsule_id])
    with pytest.raises(CorruptError):
        store.get("ab" * 32)


def test_jsonl_index_is_not_durable_and_follows_file_order(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = JsonlArtifactStore(path, "ns", [TRUSTED])
    store.init_index(["operator"])
    store.put(record("index.c"))
    store.put(record("index.a"))
    store.purge(record("index.c").capsule_id)
    reopened = JsonlArtifactStore(path, "ns", [TRUSTED])
    with pytest.raises(InvalidError):
        reopened.find()
    reopened.init_index(["operator"])
    want = [record("index.c").capsule_id, record("index.a").capsule_id]
    assert reopened.find(where={"operator": "op-a"}).ids == want


@pytest.mark.skipif(not MYSQL, reason="set CAPSULE_STORAGE_TEST_MYSQL for MySQL integration tests")
def test_mysql_index_damaged_rows():
    import pymysql

    host, _, port = MYSQL.partition(":")
    assert host == "127.0.0.1", "MySQL tests accept only a local disposable server"
    connection = pymysql.connect(host=host, port=int(port or 3306), user="root", database="capsule_storage_test")
    store = MysqlArtifactStore(connection, "py-index", [TRUSTED])
    store.init()
    with connection.cursor() as cursor:
        for table in (
            "capsule_store_index",
            "capsule_store_index_records",
            "capsule_store_index_paths",
            "capsule_store_artifacts",
            "capsule_store_capsules",
        ):
            cursor.execute(f"DELETE FROM {table} WHERE namespace='py-index'")
        store.put(record("index.a"))
        store.put(record("index.b"))
        cursor.execute(
            "UPDATE capsule_store_capsules SET capsule_bytes=X'7B' WHERE namespace='py-index' AND capsule_id=%s",
            (record("index.a").capsule_id,),
        )
    connection.commit()
    store.init_index(["operator"])
    assert store.find() == Page(ids=[record("index.b").capsule_id])
    with connection.cursor() as cursor:
        cursor.execute("UPDATE capsule_store_index_paths SET paths='{' WHERE namespace='py-index'")
    connection.commit()
    with pytest.raises(CorruptError):
        store.find()
    with pytest.raises(CorruptError):
        store.put(record("index.c"))
    store.reindex(["developer"])
    assert store.find(where={"developer": "dev-1"}) == Page(ids=[record("index.b").capsule_id])
    connection.close()
