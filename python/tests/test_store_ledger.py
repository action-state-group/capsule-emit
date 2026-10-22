# SPDX-License-Identifier: Apache-2.0
"""A ledger kept in checkpointed-local-log's managed ``LedgerStore``
(``capsule_emit.ledger_store``): the same lines, leaves and checkpoints as a
flat ``ledger.jsonl``, written only to stores capsule-emit created, read
without SQLite, crash-safe on the first write and after a torn tail."""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import stat

import pytest
from cll.ledger import LedgerStore
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import cli, ledger, ledger_store, log, seal, signing
from capsule_emit.ledger import (
    LedgerLockedError,
    _writer_lock,
    append_to_ledger,
    read_ledger,
    read_ledger_entries,
)
from capsule_emit.ledger_io import open_ledger
from capsule_emit.legacy_ledger import LegacyLedger, shared

_NO_WITNESS = "http://127.0.0.1:1"


def _cid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _seal(ledger_path, action: str = "act", **kwargs):
    return seal(None, action=action, operator="acme", anchor=False, ledger=ledger_path, witness_url=_NO_WITNESS, **kwargs)


def _segment(root) -> bytes:
    manifest = json.loads((root / "manifest.json").read_text())
    return (root / "segments" / manifest["active_segment"]).read_bytes()


@pytest.fixture
def store_dir(tmp_path):
    d = tmp_path / "capsule-ledger"
    d.mkdir()
    return d


def test_seal_into_an_empty_directory_makes_a_marked_store(store_dir):
    result = _seal(store_dir)
    assert (store_dir / "manifest.json").is_file()
    assert (store_dir / ledger_store.MARKER_FILENAME).is_file()
    assert read_ledger(store_dir) == [result.capsule]
    assert _segment(store_dir) == json.dumps(result.capsule, separators=(",", ":")).encode() + b"\n"
    assert not (store_dir / "signing_key.pem").exists()
    assert (store_dir.parent / "capsule-ledger.signing_key.pem").is_file()


def test_store_lines_and_leaves_match_a_flat_ledger(tmp_path, store_dir):
    flat = tmp_path / "ledger.jsonl"
    lines = [
        {"capsule_id": _cid("a"), "n": 1},
        {"kind": ledger.CHECKPOINT_STAMP_KIND, "v": 1, "capsule_id": _cid("stamp"), "checkpoint": {}},
        {"capsule_id": _cid("b"), "n": 2},
    ]
    for line in lines:
        append_to_ledger(line, flat)
        append_to_ledger(line, store_dir)
    assert read_ledger_entries(store_dir) == read_ledger_entries(flat)
    flat_values = [e.value for e in LegacyLedger(flat).entries()]
    store_values = [e.value for e in LegacyLedger(store_dir).entries()]
    assert store_values == flat_values
    assert [e.seq for e in LegacyLedger(store_dir).entries()] == [1, 2, 3]


def test_log_entries_go_to_the_store(store_dir):
    entry = log(b"raw", action="note", operator="acme", ledger=store_dir, witness_url=_NO_WITNESS)
    assert read_ledger(store_dir) == [entry.capsule]
    assert "signature" not in entry.capsule


def test_identical_line_is_idempotent_and_a_different_one_conflicts(store_dir):
    line = {"capsule_id": _cid("x"), "n": 1}
    assert append_to_ledger(line, store_dir) == 1
    assert append_to_ledger(dict(line), store_dir) == 1
    with pytest.raises(ledger_store.LedgerConflictError) as info:
        append_to_ledger({"capsule_id": _cid("x"), "n": 2}, store_dir)
    assert isinstance(info.value, OSError)
    assert len(read_ledger_entries(store_dir)) == 1


def test_resealing_the_same_capsule_writes_nothing(store_dir):
    first = _seal(store_dir)
    assert shared(store_dir).append_line(dict(first.capsule)).entry.seq == 1
    assert len(read_ledger(store_dir)) == 1


def test_every_spelling_of_a_store_is_one_ledger(store_dir, tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(store_dir)
    _seal(f"{store_dir}/")
    _seal(link)
    assert len(read_ledger(store_dir)) == 2
    assert signing._default_key_path(f"{store_dir}/") == signing._default_key_path(link)
    assert not (tmp_path / "alias.signing_key.pem").exists()


def test_a_flat_ledger_keeps_its_spelling_for_the_key(tmp_path):
    target = tmp_path / "data" / "ledger.jsonl"
    target.parent.mkdir()
    target.touch()
    link = tmp_path / "alias.jsonl"
    link.symlink_to(target)
    assert signing._default_key_path(link) == tmp_path / "alias.jsonl.signing_key.pem"


def test_a_store_capsule_emit_did_not_create_is_read_never_written(tmp_path):
    root = tmp_path / "foreign"
    store = LedgerStore(root, rotate_at_checkpoint=True)
    store.append({"capsule_id": _cid("f"), "n": 1})
    store.close()
    assert read_ledger_entries(root) == [{"capsule_id": _cid("f"), "n": 1}]
    with pytest.raises(ledger_store.LedgerLayoutError, match="did not create"):
        append_to_ledger({"capsule_id": _cid("g")}, root)


def test_a_directory_that_is_neither_store_nor_empty_is_refused(tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    (d / "readme.txt").write_text("hello")
    with pytest.raises(ledger_store.LedgerLayoutError):
        _seal(d)
    with pytest.raises(ledger_store.LedgerLayoutError):
        read_ledger_entries(d)


def test_an_interrupted_first_write_leaves_no_half_made_store(store_dir, monkeypatch):
    def boom(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(LedgerStore, "append", boom)
    with pytest.raises(KeyboardInterrupt):
        append_to_ledger({"capsule_id": _cid("x")}, store_dir)
    monkeypatch.undo()
    assert list(store_dir.iterdir()) == []
    assert [p.name for p in store_dir.parent.iterdir() if ".staging-" in p.name] == []
    append_to_ledger({"capsule_id": _cid("x")}, store_dir)
    assert len(read_ledger_entries(store_dir)) == 1


def test_a_new_store_path_is_created_by_init_never_as_a_file(store_dir):
    LegacyLedger(store_dir).init()
    assert (store_dir / "manifest.json").is_file() and (store_dir / ledger_store.MARKER_FILENAME).is_file()
    assert read_ledger_entries(store_dir) == []


def test_a_torn_tail_is_not_an_entry_and_the_next_write_repairs_it(store_dir):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(b'{"capsule_id":"torn')
    assert len(read_ledger_entries(store_dir)) == 1
    assert len(list(LegacyLedger(store_dir).entries())) == 1
    append_to_ledger({"capsule_id": _cid("b")}, store_dir)
    assert [e["capsule_id"] for e in read_ledger_entries(store_dir)] == [_cid("a"), _cid("b")]
    assert b"torn" not in _segment(store_dir)


def test_reads_create_nothing_and_work_on_a_read_only_copy(store_dir):
    _seal(store_dir)
    before = sorted(p.name for p in store_dir.rglob("*"))
    for path in store_dir.rglob("*"):
        os.chmod(path, stat.S_IRUSR | (stat.S_IXUSR if path.is_dir() else 0))
    os.chmod(store_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert len(read_ledger(store_dir)) == 1
        assert len(list(LegacyLedger(store_dir).entries())) == 1
    finally:
        os.chmod(store_dir, stat.S_IRWXU)
        for path in store_dir.rglob("*"):
            os.chmod(path, stat.S_IRWXU)
    assert sorted(p.name for p in store_dir.rglob("*")) == before


def test_scan_verbs_never_open_the_live_store(store_dir):
    _seal(store_dir)
    before = {p.name: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()}
    with open_ledger(store_dir) as store:
        assert len(list(store.scan())) == 1
    assert {p.name: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()} == before


def test_an_archived_segment_shows_as_a_placeholder_and_blocks_positions(tmp_path):
    from cll.checkpoint.index import MmrLedger
    from cll.ledger.segments import MmrCheckpointer

    class _FixedSigner:
        key_id = "00" * 32

        def sign(self, digest_hex: str) -> str:
            return "00" * 64

    root = tmp_path / "rotating"
    store = LedgerStore(root, rotate_at_checkpoint=True, max_segment_bytes=200)
    store.set_checkpointer(MmrCheckpointer(mmr=MmrLedger(store), signer=_FixedSigner(), log_id="demo"))
    for i in range(20):
        store.append({"capsule_id": f"{i:064x}", "payload": "x" * 40})
    first = store.list_segments()[0].name
    store.unmount_segment(first)
    store.close()

    entries = read_ledger_entries(root)
    assert entries[0]["kind"] == ledger.ARCHIVED_SEGMENT_KIND and entries[0]["record_count"] > 0
    with pytest.raises(ledger_store.LedgerArchivedError):
        list(LegacyLedger(root).entries())


def _hold(path: str, ready, release) -> None:
    from capsule_emit.ledger import _writer_lock

    with _writer_lock(__import__("pathlib").Path(path)):
        ready.put(True)
        release.wait(timeout=10)


def test_a_second_writer_process_is_refused(store_dir):
    ctx = mp.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(ledger_store.store_root(store_dir)), ready, release))
    proc.start()
    try:
        assert ready.get(timeout=10) is True
        with pytest.raises(LedgerLockedError):
            append_to_ledger({"capsule_id": _cid("x")}, store_dir)
    finally:
        release.set()
        proc.join(timeout=10)
    append_to_ledger({"capsule_id": _cid("x")}, store_dir)


def test_store_key_creation_waits_for_the_writer_lock(store_dir):
    root = ledger_store.store_root(store_dir)
    with _writer_lock(root):
        pass
    signer = signing.resolve_signer(store_dir)
    assert (root.parent / f"{root.name}.signing_key.pem").is_file()
    assert signing.resolve_signer(f"{store_dir}/") is signer


@pytest.fixture
def witnessed_store(store_dir, monkeypatch):
    url, _, stop = _start_stub_ts()
    monkeypatch.setenv("CAPSULE_WITNESS_URL", url)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")

    def _w(action, **kwargs):
        return seal(None, action=action, operator="acme", anchor=False, ledger=store_dir, witness_url=url, **kwargs).capsule

    c0 = _w("c0")
    c1 = _w("c1", confirms=c0["capsule_id"])
    assert _wait_for(lambda: _stamp_count(store_dir) >= 1)
    yield store_dir, [c0, c1]
    stop()


def test_witnessing_stamps_a_store_and_bundle_reads_it(witnessed_store, tmp_path, capsys):
    root, caps = witnessed_store
    assert LegacyLedger(root).load_cll().checkpoint is not None
    out = tmp_path / "b.json"
    code = cli.main(["bundle", str(root), "--record", caps[0]["capsule_id"], "-o", str(out)])
    err = capsys.readouterr().err
    assert code == 0, err
    assert [r["capsule_id"] for r in json.loads(out.read_bytes())["records"]] == [caps[0]["capsule_id"]]


def test_view_show_and_verify_read_a_store(store_dir, capsys):
    result = _seal(store_dir)
    ledger.view(store_dir)
    assert result.capsule["capsule_id"][:14] in capsys.readouterr().out
    assert ledger.show(store_dir, result.capsule["capsule_id"]) is True
    assert "#logged @ leaf 1" in capsys.readouterr().out
    assert cli.main(["verify", "--store", str(store_dir)]) == 0
