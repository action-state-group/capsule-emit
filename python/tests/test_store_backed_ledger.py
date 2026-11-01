# SPDX-License-Identifier: Apache-2.0
"""capsule_emit.store_backed_ledger: the Rust store-backed ledger's Python port
(capsule-emit#399), its cross-language fixture, its replay checks, its writer
checks and the import of a flat Python ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cll.checkpoint import verify_checkpoint_cose_offline
from cll.checkpoint.runner import CheckpointRunner, Ed25519CheckpointSigner
from cll.log import AppendInput
from cll.log import jsonl as cll_jsonl
from cll.log import sqlite as cll_sqlite
from cll.log.memory import MemoryStore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from capsule_emit import seal
from capsule_emit.artifact.jsonl import JsonlArtifactStore
from capsule_emit.artifact.memory import MemoryArtifactStore
from capsule_emit.artifact.record import encode_record
from capsule_emit.artifact.sqlite import SqliteArtifactStore
from capsule_emit.ledger import (
    CHECKPOINT_STAMP_KIND,
    DISCLOSURE_RECORD_KIND,
    WITNESS_BACKFILL_KIND,
    append_to_ledger,
)
from capsule_emit.legacy_ledger import LegacyLedger
from capsule_emit.signing import LocalKeypairSigner, sign_producer_envelope
from capsule_emit.store_backed_ledger import (
    PADDING_CONTENT_TYPE,
    STATEMENT,
    AlreadyInLogError,
    AppendChainMismatchError,
    ChainBrokenError,
    EnvelopeError,
    ImportRefusedError,
    Ledger,
    MalformedPaddingError,
    MissingRecordError,
    PaddingMisroutedError,
    StaleLedgerError,
    StatementInvalidError,
    StoreError,
    check_statement_matches,
    compute_record_id,
    import_flat,
    line_bytes,
    padding_record_with_nonce,
)
from capsule_emit.surface import log as unsigned_log

VECTORS = Path(__file__).resolve().parents[2] / "test-vectors" / "store-backed-ledger"
RUST = json.loads((VECTORS / "rust-written.json").read_text(encoding="utf-8"))
NAMESPACE = RUST["namespace"]
LOG_ID = "store-ledger-vectors"
ISSUER = "store-ledger-vectors"
CONTENT_TYPE = "application/vnd.agent-action-capsule+json"
PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
TRUSTED = [bytes.fromhex(k) for k in RUST["trusted"]]
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.fixture
def signer(tmp_path) -> LocalKeypairSigner:
    """The fixture's key ([7] * 32, as the Rust test's) as a capsule-emit signer."""
    path = tmp_path / "key.pem"
    path.write_bytes(PRIVATE.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return LocalKeypairSigner(path)


def memory():
    artifacts = MemoryArtifactStore(NAMESPACE, TRUSTED)
    artifacts.init()
    return MemoryStore(), artifacts


def jsonl(tmp_path):
    cll_jsonl.init(tmp_path / "log.jsonl")
    artifacts = JsonlArtifactStore(tmp_path / "artifacts.jsonl", NAMESPACE, TRUSTED)
    artifacts.init()
    return cll_jsonl.open(tmp_path / "log.jsonl"), artifacts


def sqlite(tmp_path):
    """The CLL SQLite log and the SQLite artifact store in one file, as a shelf keeps them."""
    db = tmp_path / "ledger.db"
    cll_sqlite.init(db, LOG_ID)
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False, timeout=5)
    conn.execute("PRAGMA foreign_keys=ON")
    artifacts = SqliteArtifactStore(conn, NAMESPACE, TRUSTED)
    artifacts.init()
    return cll_sqlite.open(db, LOG_ID), artifacts


def replay(steps, log, artifacts):
    ledger, _ = Ledger.open_stores(log, artifacts)
    for step in steps:
        getattr(ledger, step["op"])(step["line"], bytes.fromhex(step["statement"]))
    entries = [e.value.hex() for e in log.scan_entries(0, 1000)]
    return ledger, entries, [encode_record(artifacts.get(i)) for i in entries]


def checkpoint(log) -> dict:
    """A checkpoint cut by CLL's own runner on the log's backend."""
    runner = CheckpointRunner(log, Ed25519CheckpointSigner(PRIVATE), LOG_ID, cadence_entries=1)
    assert runner.run_once()
    decoded = verify_checkpoint_cose_offline(log.load_cll().checkpoint.bytes).decoded
    return {"mmr_size": decoded.mmr_size, "root": decoded.root}


def resign(line: dict, signer) -> dict:
    """``line`` with its capsule_id recomputed and a fresh producer envelope."""
    body = {k: v for k, v in line.items() if k not in ("signature", "key_id")}
    body["capsule_id"] = compute_record_id(body)
    body["signature"], body["key_id"] = sign_producer_envelope(signer, body["capsule_id"])
    return body


def statement(line: dict, signer, content_type: str = CONTENT_TYPE) -> bytes:
    return signer.sign_cose_statement(
        line_bytes(line), content_type=content_type, issuer=ISSUER, subject=line["capsule_id"]
    )


def rust_line(n: int) -> dict:
    return [s["line"] for s in RUST["steps"] if s["op"] == "append"][n]


# -- byte parity with Rust -------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "jsonl", "sqlite"])
def test_rust_appends_give_the_same_entries_records_and_root(tmp_path, backend):
    log, artifacts = {"memory": memory, "jsonl": lambda: jsonl(tmp_path), "sqlite": lambda: sqlite(tmp_path)}[
        backend
    ]()
    ledger, entries, records = replay(RUST["steps"], log, artifacts)
    assert entries == RUST["entries"]
    assert records == RUST["records"]
    assert checkpoint(log) == {k: RUST["checkpoint"][k] for k in ("mmr_size", "root")}

    reopened, report = Ledger.open_stores(log, artifacts)
    assert report.valid_entries == 5
    assert [line.padding for line in reopened.lines()] == [False, False, True, True, False]
    assert reopened.chain_head == ledger.chain_head == RUST["entries"][4]
    assert reopened.capsule_ids_in_order() == [RUST["entries"][i] for i in (0, 1, 4)]
    for step in RUST["steps"]:
        line = step["line"]
        assert reopened.read_line(reopened.lines()[RUST["entries"].index(line["capsule_id"])].offset) == line
        assert reopened.read_statement(line["capsule_id"]) == bytes.fromhex(step["statement"])
    entry = reopened.lookup(RUST["entries"][4])
    assert entry.capsule == rust_line(2)
    assert reopened.lookup(RUST["entries"][2]) is None  # padding is not looked up


def test_python_written_fixture_is_current(signer):
    """Python's own sequence: Rust's capsules with Python's signed statements and
    Python-built padding. Rust replays it to the same bytes
    (``rust/capsule-emit/tests/store_backed_ledger_parity.rs``)."""
    steps = [{"op": "append", "line": rust_line(0)}, {"op": "append", "line": rust_line(1)}]
    for nonce in ("cc", "dd"):
        record = padding_record_with_nonce(nonce * 32)
        record["signature"], record["key_id"] = sign_producer_envelope(signer, record["capsule_id"])
        steps.append({"op": "append_padding", "line": record})
    steps.append({"op": "append", "line": rust_line(2)})
    for step in steps:
        content_type = PADDING_CONTENT_TYPE if step["op"] == "append_padding" else CONTENT_TYPE
        step["statement"] = statement(step["line"], signer, content_type).hex()
    log, artifacts = memory()
    _, entries, records = replay(steps, log, artifacts)
    fixture = {
        "trusted": RUST["trusted"],
        "namespace": NAMESPACE,
        "steps": steps,
        "entries": entries,
        "records": records,
        "checkpoint": checkpoint(log),
    }
    text = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
    path = VECTORS / "python-written.json"
    if os.environ.get("CAPSULE_EMIT_UPDATE_VECTORS"):
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, "rerun with CAPSULE_EMIT_UPDATE_VECTORS=1"


def test_line_bytes_match_serde_json():
    # serde_json escapes only '"', '\\' and control characters, \u00XX in lowercase.
    assert line_bytes({"b": "é✓\x7f/", "a": '\x01\b\f\n\r\t"\\'}) == (
        '{"b":"é✓\x7f/","a":"\\u0001\\b\\f\\n\\r\\t\\"\\\\"}'.encode()
    )


# -- appends ---------------------------------------------------------------------


def test_append_refusals_leave_the_ledger_reopenable(signer):
    log, artifacts = memory()
    ledger, _ = Ledger.open_stores(log, artifacts)
    first = rust_line(0)
    ledger.append(first, statement(first, signer))
    with pytest.raises(AppendChainMismatchError):
        ledger.append(rust_line(2), statement(rust_line(2), signer))  # parent is not the head
    second = rust_line(1)
    with pytest.raises(StatementInvalidError):
        ledger.append(second, statement(first, signer))  # first's statement
    unsigned = {k: v for k, v in second.items() if k not in ("signature", "key_id")}
    with pytest.raises(EnvelopeError):
        ledger.append(unsigned, statement(second, signer))
    upper = dict(second, key_id=second["key_id"].upper())
    with pytest.raises((EnvelopeError, StatementInvalidError)):
        ledger.append(upper, statement(second, signer))
    padding = padding_record_with_nonce("ee" * 32)
    with pytest.raises(PaddingMisroutedError):
        ledger.append(padding, b"")
    with pytest.raises(PaddingMisroutedError):
        ledger.append_padding(second, statement(second, signer))
    with pytest.raises(MalformedPaddingError):
        ledger.append_padding(dict(padding, extra=1), b"")
    assert ledger.entries == 1
    reopened, report = Ledger.open_stores(log, artifacts)
    assert report.valid_entries == 1 and reopened.chain_head == first["capsule_id"]


def test_pad_to_bucket_pads_once_and_reopens(signer):
    log, artifacts = memory()
    ledger, _ = Ledger.open_stores(log, artifacts)
    for n in range(2):
        ledger.append(rust_line(n), statement(rust_line(n), signer))
    assert ledger.pad_to_bucket(4, signer, ISSUER) == 4
    assert ledger.pad_to_bucket(4, signer, ISSUER) == 4
    assert ledger.pad_to_bucket(0, signer, ISSUER) == 4
    assert [line.padding for line in ledger.lines()] == [False, False, True, True]
    head = ledger.chain_head
    ledger.append(rust_line(2), statement(rust_line(2), signer))  # padding never moved the head
    assert head == rust_line(1)["capsule_id"]
    reopened, report = Ledger.open_stores(log, artifacts)
    assert report.valid_entries == 5
    assert reopened.known_capsule_ids() == {rust_line(n)["capsule_id"] for n in range(3)}


def test_a_second_writer_fails_instead_of_forking(signer):
    log, artifacts = memory()
    one, _ = Ledger.open_stores(log, artifacts)
    two, _ = Ledger.open_stores(log, artifacts)
    one.append(rust_line(0), statement(rust_line(0), signer))
    with pytest.raises(StaleLedgerError):
        two.append(rust_line(0), statement(rust_line(0), signer))
    assert [e.seq for e in log.scan_entries(0, 10)] == [1]


def test_threads_share_one_ledger(signer):
    log, artifacts = memory()
    ledger, _ = Ledger.open_stores(log, artifacts, linear=False)
    lines = [resign(dict(rust_line(0), action_id=f"example/threads/{n:04}"), signer) for n in range(24)]
    for line in lines:
        line.pop("chain", None)
    lines = [resign(line, signer) for line in lines]
    errors = []

    def run(chunk):
        try:
            for line in chunk:
                ledger.append(line, statement(line, signer))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(lines[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert ledger.entries == 24 == len(log.scan_entries(0, 100))
    assert Ledger.open_stores(log, artifacts, linear=False)[1].valid_entries == 24
    with pytest.raises(AlreadyInLogError):
        ledger.append(lines[0], statement(lines[0], signer))


def test_index_keys_track_citations(signer):
    log, artifacts = memory()
    ledger, _ = Ledger.open_stores(log, artifacts, linear=False)
    foreign = "ab" * 32
    citing = dict(rust_line(0))
    citing.pop("chain", None)
    citing["references"] = [
        {
            "type": "capsule",
            "digest_alg": "SHA-256",
            "digest": foreign,
            "citation_purpose": "counterparty_half",
        }
    ]
    citing = resign(citing, signer)
    ledger.append(citing, statement(citing, signer))
    assert ledger.cites_counterparty_half(foreign)
    assert not ledger.cites_counterparty_inclusion(foreign)
    assert Ledger.open_stores(log, artifacts, linear=False)[0].cites_counterparty_half(foreign)


# -- replay checks ------------------------------------------------------------------


def _record(line: dict, statement_bytes: bytes | None):
    from capsule_emit.store_backed_ledger import _to_record

    record = _to_record(line["capsule_id"], line, statement_bytes or b"")
    if statement_bytes is None:
        record.artifacts = []
    return record


def _write(log, artifacts, record) -> None:
    artifacts.put(record)
    log.append(AppendInput(bytes.fromhex(record.capsule_id), NOW))


def test_open_refuses_a_broken_chain_unless_not_linear(signer):
    log, artifacts = memory()
    _write(log, artifacts, _record(rust_line(1), statement(rust_line(1), signer)))  # parent absent
    with pytest.raises(ChainBrokenError):
        Ledger.open_stores(log, artifacts)
    assert Ledger.open_stores(log, artifacts, linear=False)[1].valid_entries == 1


def test_open_refuses_a_missing_record_and_a_swapped_statement(signer):
    log, artifacts = memory()
    log.append(AppendInput(bytes.fromhex(rust_line(0)["capsule_id"]), NOW))
    with pytest.raises(MissingRecordError):
        Ledger.open_stores(log, artifacts)
    log, artifacts = memory()
    _write(log, artifacts, _record(rust_line(0), statement(rust_line(1), signer)))
    with pytest.raises(StatementInvalidError):
        Ledger.open_stores(log, artifacts)


def test_statements_are_required_unless_switched_off_and_purge_is_always_refused(signer):
    log, artifacts = memory()
    _write(log, artifacts, _record(rust_line(0), None))
    with pytest.raises(StatementInvalidError):
        Ledger.open_stores(log, artifacts)
    ledger, _ = Ledger.open_stores(log, artifacts, require_statements=False)
    assert ledger.read_statement(rust_line(0)["capsule_id"]) is None
    assert ledger.lookup(rust_line(0)["capsule_id"]).signed_statement is None

    log, artifacts = memory()
    _write(log, artifacts, _record(rust_line(0), statement(rust_line(0), signer)))
    artifacts.purge(rust_line(0)["capsule_id"])
    assert [a.name for a in artifacts.get(rust_line(0)["capsule_id"]).artifacts] == [STATEMENT]
    with pytest.raises(StatementInvalidError):
        Ledger.open_stores(log, artifacts, require_statements=False)


# -- importing a flat Python ledger ---------------------------------------------------


@pytest.fixture
def flat(tmp_path, monkeypatch):
    """A Goose-shaped flat ledger: a join, a request and an action confirming
    it, and an unchained record, with a checkpoint stamp between them."""
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    path = tmp_path / "goose" / "ledger.jsonl"
    path.parent.mkdir()
    path.touch()
    kw = {"operator": "goose-user", "anchor": False, "witness": False, "ledger": path}
    join = seal({"join": "goose-user"}, action="loop.join", **kw)
    request = seal({"ask": 1}, action="loop.request", **kw)
    append_to_ledger({"kind": CHECKPOINT_STAMP_KIND, "capsule_id": "cd" * 32, "checkpoint": {}}, path)
    seal({"done": 1}, action="loop.action", confirms=request.capsule["capsule_id"], **kw)
    seal({"join": "goose-user"}, action="loop.join", **kw)  # the same content again: one record
    assert join.capsule["capsule_id"]
    return path


def _trusted_store(path):
    key = LegacyLedger(path).get(json.loads(path.read_text().splitlines()[0])["capsule_id"])
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    public = verify_producer_envelope(key.capsule_id, key.producer_envelope).public_key
    artifacts = MemoryArtifactStore(NAMESPACE, [public])
    artifacts.init()
    return MemoryStore(), artifacts


def test_import_keeps_every_record_as_stored_and_reruns(flat):
    log, artifacts = _trusted_store(flat)
    ledger, report = import_flat(flat, log, artifacts)
    lines = [json.loads(t) for t in flat.read_text().splitlines()]
    capsules = [o for o in lines if "kind" not in o]
    ids = list(dict.fromkeys(o["capsule_id"] for o in capsules))
    assert [e.value.hex() for e in log.scan_entries(0, 100)] == ids
    legacy = LegacyLedger(flat)
    for capsule_id in ids:
        assert encode_record(artifacts.get(capsule_id)) == encode_record(legacy.get(capsule_id))
    assert report.records == len(ids) == ledger.entries
    assert report.skipped == {
        CHECKPOINT_STAMP_KIND: 1,
        **({"repeat": len(capsules) - len(ids)} if len(capsules) > len(ids) else {}),
    }
    assert ledger.lookup(ids[0]).capsule == {**capsules[0]}
    assert ledger.lookup(ids[0]).signed_statement is None

    again, rerun = import_flat(flat, log, artifacts)
    assert rerun.resumed == len(ids) and again.entries == len(ids)


def test_import_resumes_after_a_crash_between_put_and_append(flat):
    log, artifacts = _trusted_store(flat)
    first = LegacyLedger(flat).get(json.loads(flat.read_text().splitlines()[0])["capsule_id"])
    artifacts.put(first)  # the record landed, its entry did not
    _, report = import_flat(flat, log, artifacts)
    assert report.resumed == 0 and report.records == len(log.scan_entries(0, 100))


def test_import_refuses_lines_no_store_record_keeps(flat, tmp_path):
    for extra in ("log", "disclosure", "resigned"):
        path = tmp_path / f"{extra}.jsonl"
        path.write_bytes(flat.read_bytes())
        if extra == "resigned":
            first = json.loads(flat.read_text().splitlines()[0])
            # An old writer's second, different line under one capsule_id (writers
            # refuse it now). Flip the last hex digit so it always differs.
            last = first["signature"][-1]
            changed = first["signature"][:-1] + ("1" if last == "0" else "0")
            with path.open("a") as fh:
                fh.write(json.dumps(dict(first, signature=changed), separators=(",", ":")) + "\n")
        elif extra == "log":
            unsigned_log(b"unsigned bytes", ledger=path)
        else:
            append_to_ledger({"kind": DISCLOSURE_RECORD_KIND, "capsule_id": "ef" * 32}, path)
        log, artifacts = _trusted_store(flat)
        with pytest.raises(ImportRefusedError) as refused:
            import_flat(path, log, artifacts)
        assert refused.value.lines == [len(path.read_text().splitlines())]
        assert log.scan_entries(0, 10) == []


def test_import_refuses_to_resume_over_a_different_log(flat, signer):
    log, goose = _trusted_store(flat)
    artifacts = MemoryArtifactStore(NAMESPACE, [*TRUSTED, *goose._trusted])
    artifacts.init()
    _write(log, artifacts, _record(rust_line(0), statement(rust_line(0), signer)))
    with pytest.raises(ImportRefusedError):
        import_flat(flat, log, artifacts)


def test_pad_to_bucket_is_one_run_while_other_threads_append(signer):
    log, artifacts = memory()
    ledger, _ = Ledger.open_stores(log, artifacts, linear=False)
    lines = []
    for n in range(12):
        line = dict(rust_line(0), action_id=f"example/pad-threads/{n:04}")
        line.pop("chain", None)
        lines.append(resign(line, signer))

    def appends():
        for line in lines:
            ledger.append(line, statement(line, signer))

    def pads():
        for _ in range(6):
            ledger.pad_to_bucket(4, signer, ISSUER)

    threads = [threading.Thread(target=appends), threading.Thread(target=pads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    padding = [line.padding for line in ledger.lines()]
    # Every run of padding records ends on a bucket boundary: no append landed inside one.
    for end in range(1, len(padding) + 1):
        if padding[end - 1] and (end == len(padding) or not padding[end]):
            assert end % 4 == 0
    assert padding.count(False) == 12


def test_import_of_a_missing_source_raises(tmp_path):
    log, artifacts = memory()
    with pytest.raises(FileNotFoundError):
        import_flat(tmp_path / "nope.jsonl", log, artifacts)


def test_import_from_an_untrusted_signer_writes_no_entry(flat):
    log, artifacts = memory()  # trusts only the fixture key, not the Goose key
    with pytest.raises(StoreError):
        import_flat(flat, log, artifacts)
    assert log.scan_entries(0, 10) == []
    assert Ledger.open_stores(log, artifacts)[0].entries == 0


def test_import_from_a_ledger_store_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    store = tmp_path / "goose-store"
    store.mkdir()
    kw = {"operator": "goose-user", "anchor": False, "witness": False, "ledger": store}
    first = seal({"join": "goose-user"}, action="loop.join", **kw)
    seal({"ask": 1}, action="loop.request", confirms=first.capsule["capsule_id"], **kw)
    legacy = LegacyLedger(store)
    ids = [e.value.hex() for e in legacy.scan_entries(0, 10)]
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    record = legacy.get(ids[0])
    artifacts = MemoryArtifactStore(
        NAMESPACE, [verify_producer_envelope(record.capsule_id, record.producer_envelope).public_key]
    )
    artifacts.init()
    log = MemoryStore()
    ledger, report = import_flat(store, log, artifacts)
    assert [e.value.hex() for e in log.scan_entries(0, 10)] == ids
    assert all(encode_record(artifacts.get(i)) == encode_record(legacy.get(i)) for i in ids)
    assert report.records == 2 and ledger.chain_head == ids[1]


def test_import_refuses_a_non_object_line_and_skips_backfills(flat, tmp_path):
    path = tmp_path / "odd.jsonl"
    path.write_bytes(flat.read_bytes() + b"123\n")
    log, artifacts = _trusted_store(flat)
    with pytest.raises(ImportRefusedError) as refused:
        import_flat(path, log, artifacts)
    # LegacyLedger reads the whole file as corrupt, so its records refuse too.
    assert refused.value.lines[-1] == len(path.read_text().splitlines())
    assert log.scan_entries(0, 10) == []

    backfill = tmp_path / "backfill.jsonl"
    backfill.write_bytes(flat.read_bytes())
    append_to_ledger({"kind": WITNESS_BACKFILL_KIND, "capsule_id": "aa" * 32}, backfill)
    _, report = import_flat(backfill, *_trusted_store(flat))
    assert report.skipped[WITNESS_BACKFILL_KIND] == 1


def test_statement_check_takes_only_cose_sign1_and_profile_critical_headers(signer):
    import cbor2

    line = {k: v for k, v in rust_line(0).items() if k != "key_id"}
    good = statement(rust_line(0), signer)
    message = cbor2.loads(good)
    assert check_statement_matches(line, line["capsule_id"], good) is None
    other_tag = cbor2.dumps(cbor2.CBORTag(98, message.value))
    assert "COSE_Sign1" in check_statement_matches(line, line["capsule_id"], other_tag)
    # A statement marking its content type critical verifies, as in Rust.
    protected = cbor2.loads(message.value[0])
    protected[2] = [3]
    header = cbor2.dumps(protected, canonical=True)
    to_sign = cbor2.dumps(["Signature1", header, b"", message.value[2]], canonical=True)
    resigned = cbor2.dumps(cbor2.CBORTag(18, [header, {}, message.value[2], PRIVATE.sign(to_sign)]))
    assert check_statement_matches(rust_line(0), line["capsule_id"], resigned) is None
