# SPDX-License-Identifier: Apache-2.0
"""Artifact store: the shared conformance suite on every backend, the
cross-language codec and checksum goldens, and Python-specific behavior.

MySQL tests opt in through ``CAPSULE_STORAGE_TEST_MYSQL`` (host:port of a
disposable MySQL 8.4 with database ``capsule_storage_test`` and passwordless
``root``), and accept only ``127.0.0.1``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from capsule_emit.artifact import (
    PURGED,
    Artifact,
    CorruptError,
    InvalidError,
    NotFoundError,
    Store,
    TxStore,
    conformance,
    storage_checksum,
)
from capsule_emit.artifact.jsonl import JsonlArtifactStore, decode_record, encode_record
from capsule_emit.artifact.memory import MemoryArtifactStore
from capsule_emit.artifact.mysql import MysqlArtifactStore
from capsule_emit.artifact.record import copy_record, prepare, verify
from capsule_emit.artifact.sqlite import SqliteArtifactStore

REPO = Path(__file__).resolve().parents[2]
VECTORS = conformance.load_vectors()
TRUSTED = bytes.fromhex(VECTORS["keys"]["trusted"])
MYSQL = os.environ.get("CAPSULE_STORAGE_TEST_MYSQL", "")
CONFORMANCE_NAMESPACES = ("conformance", "alpha", "beta")


def record(name):
    return conformance._record(VECTORS, name)


def sqlite_connection(tmp_path_factory):
    connection = sqlite3.connect(tmp_path_factory.mktemp("sqlite") / "artifacts.db")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def mysql_connection():
    import pymysql

    host, _, port = MYSQL.partition(":")
    assert host == "127.0.0.1", "MySQL tests accept only a local disposable server"
    return pymysql.connect(host=host, port=int(port or 3306), user="root", database="capsule_storage_test")


def mysql_backing():
    connection = mysql_connection()
    MysqlArtifactStore(connection, "conformance", [TRUSTED]).init()
    with connection.cursor() as cursor:
        for table in ("capsule_store_artifacts", "capsule_store_capsules"):
            cursor.execute(f"DELETE FROM {table} WHERE namespace IN (%s,%s,%s)", CONFORMANCE_NAMESPACES)
    connection.commit()
    return connection


def harnesses(tmp_path_factory):
    yield "memory", conformance.Harness(
        new_backing=lambda: None, open=lambda _b, ns, keys: MemoryArtifactStore(ns, keys)
    )
    yield "jsonl", conformance.Harness(
        new_backing=lambda: tmp_path_factory.mktemp("jsonl") / "artifacts.jsonl",
        open=lambda path, ns, keys: JsonlArtifactStore(path, ns, keys),
        durable=True,
    )
    yield "sqlite", conformance.Harness(
        new_backing=lambda: sqlite_connection(tmp_path_factory),
        open=lambda conn, ns, keys: SqliteArtifactStore(conn, ns, keys),
        durable=True,
        shared_backing=True,
        close=lambda conn: conn.close(),
    )
    yield "mysql", conformance.Harness(
        new_backing=mysql_backing,
        open=lambda conn, ns, keys: MysqlArtifactStore(conn, ns, keys),
        durable=True,
        shared_backing=True,
        close=lambda conn: conn.close(),
    )


def _conformance_params():
    names = [t.name for t in conformance.tests(conformance.Harness(lambda: None, lambda *a: None), VECTORS)]
    return [(backend, name) for backend in ("memory", "jsonl", "sqlite", "mysql") for name in names]


@pytest.mark.parametrize(("backend", "name"), _conformance_params())
def test_conformance(backend, name, tmp_path_factory):
    if backend == "mysql" and not MYSQL:
        pytest.skip("set CAPSULE_STORAGE_TEST_MYSQL for MySQL integration tests")
    harness = dict(harnesses(tmp_path_factory))[backend]
    test = next(t for t in conformance.tests(harness, VECTORS) if t.name == name)
    if test.skip:
        pytest.skip(test.skip)
    test.run()


def test_run_reports_every_outcome():
    outcomes = conformance.run(
        conformance.Harness(new_backing=lambda: None, open=lambda _b, ns, keys: MemoryArtifactStore(ns, keys))
    )
    assert {o.status for o in outcomes} == {"pass", "skip"}
    broken = conformance.run(conformance.Harness(new_backing=lambda: None, open=lambda *_: BrokenStore()))
    assert any(o.status == "fail" for o in broken)


class BrokenStore(MemoryArtifactStore):
    """Accepts any configuration and never stores anything."""

    def __init__(self):
        super().__init__("conformance", [TRUSTED])

    def put(self, record):
        return None


# ---- Codec, checksum and DDL goldens shared with Go and TypeScript ---------


def test_vectors_copy_is_byte_identical():
    bundled = Path(conformance.__file__).with_name("conformance_vectors.json").read_bytes()
    assert bundled == (REPO / "test-vectors" / "artifact-store" / "vectors.json").read_bytes()


@pytest.mark.parametrize("vector", VECTORS["checksums"], ids=lambda v: v["record"])
def test_storage_checksum_goldens(vector):
    assert storage_checksum(record(vector["record"])) == vector["storage_checksum"]


def test_storage_checksum_pins_the_go_and_ts_goldens():
    assert storage_checksum(record("synthetic.stored")) == (
        "ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9"
    )
    assert storage_checksum(record("synthetic.empty")) == (
        "33be5d36fbe3fcd89e5ec0a3ac5feb0ea764fa08053c34d0978c99fc2dfb71f4"
    )


@pytest.mark.parametrize("vector", VECTORS["lines"], ids=lambda v: v["record"])
def test_jsonl_line_goldens(vector):
    assert encode_record(record(vector["record"])) == vector["line"]
    assert conformance.diff(record(vector["record"]), decode_record(vector["line"])) == ""


@pytest.mark.parametrize(
    ("module", "backend"),
    [(SqliteArtifactStore, "sqlite"), (MysqlArtifactStore, "mysql")],
)
def test_ddl_matches_go_and_typescript(module, backend):
    import importlib

    schema = importlib.import_module(module.__module__).SCHEMA
    go_schema = (REPO / "go" / "artifact" / backend / "schema.sql").read_text(encoding="utf-8")
    assert schema.split() == go_schema.split()


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "null",
        "[]",
        '{"capsule_id":"a","capsule":"e30=","producer_envelope":"AA==","artifacts":{}}',
        '{"capsule_id":"a","capsule":"e30=","producer_envelope":"AA==","artifacts":[null]}',
        '{"capsule_id":"a","capsule":"e30=","producer_envelope":"AA==","artifacts":[{"name":"x"}]}',
        '{"capsule_id":"a","capsule":"!!","producer_envelope":"AA==","artifacts":[]}',
        '{"capsule_id":"a","capsule":"e30=","producer_envelope":1,"artifacts":[]}',
    ],
)
def test_decode_rejects_malformed_lines(line):
    with pytest.raises(CorruptError):
        decode_record(line)


# ---- Packaging -------------------------------------------------------------


def test_importing_the_package_loads_no_database_driver():
    code = (
        "import sys, capsule_emit, capsule_emit.artifact, capsule_emit.artifact.mysql, "
        "capsule_emit.artifact.jsonl, capsule_emit.artifact.memory, capsule_emit.artifact.conformance\n"
        "assert 'pymysql' not in sys.modules and 'MySQLdb' not in sys.modules, sorted(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_protocols():
    memory = MemoryArtifactStore("ns", [TRUSTED])
    assert isinstance(memory, Store) and not isinstance(memory, TxStore)
    assert isinstance(SqliteArtifactStore(sqlite3.connect(":memory:"), "ns", [TRUSTED]), TxStore)


# ---- Backend-specific behavior ---------------------------------------------


def stored_jsonl(tmp_path):
    path = tmp_path / "artifacts.jsonl"
    store = JsonlArtifactStore(path, "ns", [TRUSTED])
    store.init()
    store.put(record("basic"))
    return store, path


def test_jsonl_put_requires_init(tmp_path):
    store = JsonlArtifactStore(tmp_path / "missing.jsonl", "ns", [TRUSTED])
    with pytest.raises(FileNotFoundError):
        store.put(record("basic"))


def test_jsonl_reads_lines_written_by_other_languages(tmp_path):
    """A file of golden lines, as the Go and TypeScript stores write them,
    opens and verifies here, and purging rewrites only the target line."""
    path = tmp_path / "artifacts.jsonl"
    lines = [v["line"] for v in VECTORS["lines"] if v["record"] in ("basic.stored", "blank.stored")]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    store = JsonlArtifactStore(path, "ns", [TRUSTED])
    basic_id = record("basic").capsule_id
    assert conformance.diff(record("basic.stored"), store.get(basic_id)) == ""
    store.purge(basic_id)
    text = path.read_text(encoding="utf-8").splitlines()
    assert text[0] == next(v["line"] for v in VECTORS["lines"] if v["record"] == "basic.purged")
    assert text[1] == lines[1]


def test_jsonl_fails_closed_on_damaged_lines(tmp_path):
    store, path = stored_jsonl(tmp_path)
    basic_id = record("basic").capsule_id
    path.write_bytes(path.read_bytes()[:20])
    with pytest.raises(CorruptError):
        store.get(basic_id)
    path.write_bytes(b"x" * 4000 + b"\n")
    with pytest.raises(CorruptError):
        store.get(basic_id)


def test_jsonl_index_pointing_at_another_record_is_corrupt(tmp_path):
    store, path = stored_jsonl(tmp_path)
    other = JsonlArtifactStore(tmp_path / "other.jsonl", "ns", [TRUSTED])
    other.init()
    other.put(record("empty"))
    (tmp_path / "other.jsonl").replace(path)
    with pytest.raises(CorruptError):
        store.get(record("basic").capsule_id)


def test_jsonl_purge_keeps_unparseable_and_torn_lines_and_permissions(tmp_path):
    store, path = stored_jsonl(tmp_path)
    os.chmod(path, 0o640)
    with open(path, "ab") as f:
        f.write(b"garbage\n\n{\"capsule_id\":")
    reopened = JsonlArtifactStore(path, "ns", [TRUSTED])
    reopened.purge(record("basic").capsule_id)
    data = path.read_bytes()
    assert data.endswith(b"garbage\n\n{\"capsule_id\":")
    assert os.stat(path).st_mode & 0o777 == 0o640
    assert reopened.get(record("basic").capsule_id).artifacts[0].state == PURGED


def test_jsonl_purge_of_vanished_line_is_corrupt(tmp_path):
    store, path = stored_jsonl(tmp_path)
    path.write_bytes(b"\n" * (len(path.read_bytes())))
    with pytest.raises(CorruptError):
        store.purge(record("basic").capsule_id)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".jsonl-rewrite-")]


def test_sqlite_rejects_tampered_rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "a.db")
    connection.execute("PRAGMA foreign_keys=ON")
    store = SqliteArtifactStore(connection, "ns", [TRUSTED])
    store.init()
    store.put(record("basic"))
    basic_id = record("basic").capsule_id
    connection.execute("DELETE FROM capsule_store_artifacts WHERE name='private_note'")
    connection.commit()
    with pytest.raises(CorruptError):
        store.get(basic_id)
    connection.execute(
        "INSERT INTO capsule_store_artifacts (namespace,capsule_id,name,digest_field,content_bytes,"
        "content_sha256,retention_state) VALUES('ns',?,'private_note','','text',?,'present')",
        (basic_id, "0" * 64),
    )
    connection.commit()
    with pytest.raises(CorruptError, match="BLOB"):
        store.get(basic_id)


def test_sqlite_transactional_extension(tmp_path):
    connection = sqlite3.connect(tmp_path / "a.db", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    store = SqliteArtifactStore(connection, "ns", [TRUSTED])
    store.init()
    connection.execute("BEGIN")
    store.put_tx(connection, record("basic"))
    assert store.get_tx(connection, record("basic").capsule_id, lock=True).capsule_id
    connection.execute("ROLLBACK")
    with pytest.raises(NotFoundError):
        store.get(record("basic").capsule_id)
    with pytest.raises(InvalidError):
        store.put_tx(None, record("basic"))
    with pytest.raises(InvalidError):
        store.get_tx(None, record("basic").capsule_id)
    with pytest.raises(InvalidError):
        SqliteArtifactStore(None, "ns", [TRUSTED])


def test_sqlite_put_rolls_back_on_failure(tmp_path):
    connection = sqlite3.connect(tmp_path / "a.db")
    store = SqliteArtifactStore(connection, "ns", [TRUSTED])
    store.init()
    connection.execute("DROP TABLE capsule_store_artifacts")
    with pytest.raises(sqlite3.OperationalError):
        store.put(record("basic"))
    assert not connection.in_transaction
    assert connection.execute("SELECT COUNT(*) FROM capsule_store_capsules").fetchone() == (0,)


@pytest.mark.skipif(not MYSQL, reason="set CAPSULE_STORAGE_TEST_MYSQL for MySQL integration tests")
def test_mysql_transactional_extension_and_lock():
    connection = mysql_connection()
    store = MysqlArtifactStore(connection, "py-tx", [TRUSTED])
    store.init()
    with connection.cursor() as cursor:
        for table in ("capsule_store_artifacts", "capsule_store_capsules"):
            cursor.execute(f"DELETE FROM {table} WHERE namespace='py-tx'")
    connection.commit()
    connection.begin()
    store.put_tx(connection, record("basic"))
    assert store.get_tx(connection, record("basic").capsule_id, lock=True).capsule_id
    connection.rollback()
    with pytest.raises(NotFoundError):
        store.get(record("basic").capsule_id)
    store.put(record("basic"))
    with connection.cursor() as cursor:
        cursor.execute("UPDATE capsule_store_capsules SET record_sha256=%s WHERE namespace='py-tx'", ("0" * 64,))
    connection.commit()
    with pytest.raises(CorruptError):
        store.get(record("basic").capsule_id)
    connection.close()


def test_mysql_deadlock_retry_is_bounded(monkeypatch):
    class Deadlock(Exception):
        pass

    calls = []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def rollback(self):
            pass

        def commit(self):
            pass

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args):
            pass

    store = MysqlArtifactStore(FakeConnection(), "ns", [TRUSTED])

    def put_tx(tx, record):
        calls.append(1)
        raise Deadlock(1213, "deadlock")

    monkeypatch.setattr(store, "put_tx", put_tx)
    monkeypatch.setattr("capsule_emit.artifact.mysql.time.sleep", lambda _s: None)
    with pytest.raises(Deadlock):
        store.put(record("basic"))
    assert len(calls) == 5


def test_prepare_does_not_alias_and_verify_reports_bindings():
    original = record("basic")
    prepared = prepare(original)
    prepared.artifacts[0].name = "changed"
    assert original.artifacts[0].name == "payload"
    checks = verify(prepared_copy := copy_record(prepare(original)), [TRUSTED])
    assert checks["payload"].verified and not checks["private_note"].bound
    assert prepared_copy.artifacts[2].content_sha256


def test_verify_rejects_capsules_that_do_not_decode():
    basic = prepare(record("basic"))
    for capsule in (b"\xff", b'{"a":1,"a":2}', b"[]", b"{} {}", b'{"x":NaN}', b"[" * 1001 + b"]" * 1001):
        broken = copy_record(basic)
        broken.capsule = capsule
        with pytest.raises(InvalidError):
            verify(broken, [TRUSTED])


def test_verify_rejects_unsafe_digest_inputs():
    basic = record("basic")
    for content in (b'{"n":1.5}', b'{"a":1,"a":1}', b"\xff"):
        broken = copy_record(basic)
        broken.artifacts[0] = Artifact(name="payload", state="present", binding=basic.artifacts[0].binding, content=content)
        with pytest.raises(Exception) as info:
            verify(prepare(broken), [TRUSTED])
        assert getattr(info.value, "code", "") == "digest_mismatch"
