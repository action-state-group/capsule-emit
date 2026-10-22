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
import tempfile
from pathlib import Path

import pytest
from cll.ledger import LedgerStore
from cll.log import conformance as cll_conformance
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import cli, ledger, ledger_store, log, seal, signing
from capsule_emit.artifact import conformance as artifact_conformance
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


# -- the same conformance subsets as the flat ledger, on a store --------------


def _fresh_store():
    d = Path(tempfile.mkdtemp()) / "capsule-ledger"
    d.mkdir()
    return d


def _reopen(current):
    current.close()
    return LegacyLedger(current._path)


STORE_CLL_CASES = cll_conformance.cases(
    lambda: LegacyLedger(_fresh_store()), _reopen, digest_append=False, interfaces=("entries", "close")
)


@pytest.mark.parametrize("case", STORE_CLL_CASES, ids=[c.name for c in STORE_CLL_CASES])
def test_cll_conformance_subset_on_a_store(case):
    if case.skip:
        pytest.skip(case.skip)
    case.run()


STORE_HARNESS = artifact_conformance.Harness(
    new_backing=_fresh_store,
    open=lambda path, namespace, trusted: LegacyLedger(path, namespace, trusted),
    durable=True,
    originals=False,
    purge=False,
)
STORE_ARTIFACT_TESTS = artifact_conformance.tests(STORE_HARNESS)


@pytest.mark.parametrize("test", STORE_ARTIFACT_TESTS, ids=[t.name for t in STORE_ARTIFACT_TESTS])
def test_artifact_conformance_subset_on_a_store(test):
    if test.skip:
        pytest.skip(test.skip)
    test.run()


# -- review round 1 ------------------------------------------------------------


def test_the_first_write_keeps_the_directory_mode_and_group(store_dir):
    os.chmod(store_dir, 0o2775)
    group = store_dir.stat().st_gid
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    assert stat.S_IMODE(store_dir.stat().st_mode) == 0o2775
    assert store_dir.stat().st_gid == group


def test_a_mount_point_is_refused_with_a_clear_message(store_dir, monkeypatch):
    real = os.path.ismount
    monkeypatch.setattr(ledger_store.os.path, "ismount", lambda p: os.fspath(p) == os.fspath(ledger_store.store_root(store_dir)) or real(p))
    with pytest.raises(ledger_store.LedgerLayoutError, match="mount point"):
        append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    assert list(store_dir.iterdir()) == []


@pytest.mark.parametrize("line", [{"capsule_id": "short"}, {"no": "id"}, {"capsule_id": "AB" * 32}])
def test_append_to_ledger_refuses_bad_lines_as_oserror(store_dir, tmp_path, line):
    for target in (store_dir, tmp_path / "ledger.jsonl"):
        with pytest.raises(ledger_store.LedgerWriteError) as info:
            append_to_ledger(line, target)
        assert isinstance(info.value, OSError)


def test_append_to_ledger_reports_a_corrupt_store_as_oserror(store_dir):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(b"not json\n")
    with pytest.raises(OSError):
        append_to_ledger({"capsule_id": _cid("b")}, store_dir)


def test_scan_verbs_leave_an_empty_directory_empty(store_dir):
    with open_ledger(store_dir) as store:
        assert list(store.scan()) == []
    assert list(store_dir.iterdir()) == []
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)


def _archived_store(tmp_path):
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
    store.unmount_segment(store.list_segments()[0].name)
    store.close()
    return root


def test_positions_and_proofs_refuse_an_archived_store(tmp_path, capsys):
    from capsule_emit.record_proof import record_proof

    root = _archived_store(tmp_path)
    last = f"{19:064x}"
    with pytest.raises(ledger_store.LedgerArchivedError):
        record_proof(root, last)
    with pytest.raises(ledger_store.LedgerArchivedError):
        with open_ledger(root):
            pass
    assert ledger.show(root, last) is True
    assert "#logged @ leaf" not in capsys.readouterr().out


def test_a_bad_line_fails_every_read_not_just_the_first(store_dir):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(b'{"capsule_id":"short"}\n' + json.dumps({"capsule_id": _cid("c")}).encode() + b"\n")
    reader = LegacyLedger(store_dir)
    for _ in range(2):
        with pytest.raises(Exception, match="32-byte"):
            list(reader.entries())


def test_an_idempotent_retry_after_a_crash_finds_the_line_indexed(store_dir):
    line = {"capsule_id": _cid("a")}
    append_to_ledger({"capsule_id": _cid("first")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(json.dumps(line, separators=(",", ":")).encode() + b"\n")
    assert append_to_ledger(dict(line), store_dir) == 2
    store = LedgerStore(store_dir, rotate_at_checkpoint=True)
    try:
        assert store.fetch(_cid("a")).seq == 2
    finally:
        store.close()


def _resolve_in_child(path: str, done) -> None:
    from capsule_emit import signing as child_signing

    child_signing.resolve_signer(path)
    done.set()


def test_store_key_creation_waits_for_a_held_writer_lock(store_dir):
    ctx = mp.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(ledger_store.store_root(store_dir)), ready, release))
    holder.start()
    key = signing._default_key_path(store_dir)
    try:
        assert ready.get(timeout=10) is True
        done = ctx.Event()
        resolver = ctx.Process(target=_resolve_in_child, args=(str(store_dir), done))
        resolver.start()
        assert not done.wait(timeout=1.0)
        assert not key.exists()
    finally:
        release.set()
        holder.join(timeout=10)
    assert done.wait(timeout=10)
    resolver.join(timeout=10)
    assert key.is_file()


# -- review round 2 ------------------------------------------------------------


@pytest.mark.parametrize("line", [["not", "a", "dict"], {"capsule_id": "ab" * 32, "x": b"bytes"}])
def test_append_to_ledger_refuses_unwritable_values_as_oserror(store_dir, line):
    with pytest.raises(ledger_store.LedgerWriteError):
        append_to_ledger(line, store_dir)


def test_a_transient_read_failure_is_retried_whole(store_dir, monkeypatch):
    for name in ("a", "b", "c"):
        append_to_ledger({"capsule_id": _cid(name)}, store_dir)
    reader = LegacyLedger(store_dir)
    real = ledger_store.StoreLines.refresh
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(self)

    monkeypatch.setattr(ledger_store.StoreLines, "refresh", flaky)
    with pytest.raises(OSError):
        list(reader.entries())
    assert len(list(reader.entries())) == 3


def test_an_append_while_a_thread_creates_the_store_key_waits(store_dir, monkeypatch):
    import threading
    import time

    real_init = signing.LocalKeypairSigner.__init__
    started = threading.Event()

    def slow_init(self, key_path):
        started.set()
        time.sleep(0.5)
        real_init(self, key_path)

    monkeypatch.setattr(signing.LocalKeypairSigner, "__init__", slow_init)
    resolver = threading.Thread(target=signing.resolve_signer, args=(store_dir,))
    resolver.start()
    assert started.wait(timeout=5)
    append_to_ledger({"capsule_id": _cid("during")}, store_dir)
    resolver.join(timeout=10)
    assert [e["capsule_id"] for e in read_ledger_entries(store_dir)] == [_cid("during")]


def test_a_repeated_line_is_reported_when_read(store_dir, caplog):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(json.dumps({"capsule_id": _cid("a")}, separators=(",", ":")).encode() + b"\n")
    with caplog.at_level("WARNING"):
        assert len(read_ledger_entries(store_dir)) == 1
    assert "repeating capsule_id" in caplog.text


# -- review round 2b -----------------------------------------------------------


def test_a_failing_writer_lock_in_resolve_signer_releases_the_append_lock(store_dir, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("read-only parent")

    monkeypatch.setattr(ledger, "_writer_lock", fail)
    with pytest.raises(PermissionError):
        signing.resolve_signer(store_dir)
    assert not ledger._append_lock.locked()


def test_an_invalid_manifest_is_refused_before_anything_changes(store_dir):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    (store_dir / ledger_store.MARKER_FILENAME).write_text('{"v":1}')
    manifest = json.loads((store_dir / "manifest.json").read_text())
    manifest["active_segment"] = None
    (store_dir / "manifest.json").write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()}
    with pytest.raises(OSError):
        append_to_ledger({"capsule_id": _cid("b")}, store_dir)
    assert {p.name: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()} == before


def test_an_unterminated_closed_segment_is_refused_by_every_reader(tmp_path):
    root = _archived_store(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    first = manifest["segments"][0]["name"]
    archived = root / "segments" / ".archived" / first
    (root / "segments" / first).write_bytes(archived.read_bytes()[:-1])
    archived.unlink()
    for entry in manifest["segments"]:
        entry["mounted"] = True
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ledger_store.LedgerLayoutError, match="unterminated"):
        read_ledger_entries(root)
    with pytest.raises(ledger_store.LedgerLayoutError, match="unterminated"):
        list(LegacyLedger(root).entries())


def test_a_repeated_line_is_reported_through_the_proof_reader_too(store_dir, caplog):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(json.dumps({"capsule_id": _cid("a")}, separators=(",", ":")).encode() + b"\n")
    with caplog.at_level("WARNING"):
        assert len(list(LegacyLedger(store_dir).entries())) == 1
    assert "repeating capsule_id" in caplog.text


def test_a_store_deleted_and_recreated_is_read_afresh(store_dir):
    import shutil

    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    shared(store_dir).entries()
    shutil.rmtree(store_dir)
    store_dir.mkdir()
    append_to_ledger({"capsule_id": _cid("bb"), "padding": "x" * 200}, store_dir)
    assert [e.value.hex() for e in shared(store_dir).entries()] == [_cid("bb")]


# -- review round 3 ------------------------------------------------------------


def test_another_reader_sees_a_recreated_store_afresh(store_dir):
    import shutil

    append_to_ledger({"capsule_id": _cid("old")}, store_dir)
    reader = LegacyLedger(store_dir)
    assert [e.value.hex() for e in reader.entries()] == [_cid("old")]
    shutil.rmtree(store_dir)
    store_dir.mkdir()
    for name in ("new1", "new2"):
        append_to_ledger({"capsule_id": _cid(name), "padding": "x" * 300}, store_dir)
    assert [e.value.hex() for e in reader.entries()] == [_cid("new1"), _cid("new2")]


def test_a_group_that_cannot_be_kept_refuses_the_first_write(store_dir, monkeypatch):
    others = [g for g in os.getgroups() if g != store_dir.parent.stat().st_gid]
    if not others:
        pytest.skip("needs membership of a second group")
    os.chown(store_dir, -1, others[0])

    def fail(*args, **kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(ledger_store.os, "chown", fail)
    with pytest.raises(ledger_store.LedgerLayoutError, match="group"):
        append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    assert list(store_dir.iterdir()) == []


def test_scan_verbs_keep_stored_metadata_and_correlation_fields(tmp_path):
    root = tmp_path / "s"
    store = LedgerStore(root, rotate_at_checkpoint=True, correlation_fields=("exchange_id",))
    store.append({"capsule_id": _cid("a"), "exchange_id": "ex-1"}, consequential=True)
    store.close()
    with open_ledger(root) as scan:
        records = list(scan.scan())
        assert [r.consequential for r in records] == [True]
        assert [r.capsule_id for r in scan.by_correlation("ex-1")] == [_cid("a")]
    assert not any(p.name.endswith(("-wal", "-shm")) for p in root.iterdir())



# -- review round 4 ------------------------------------------------------------


def test_scan_verbs_see_a_line_a_crashed_writer_left(store_dir):
    append_to_ledger({"capsule_id": _cid("a")}, store_dir)
    manifest = json.loads((store_dir / "manifest.json").read_text())
    with open(store_dir / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(json.dumps({"capsule_id": _cid("crashed")}, separators=(",", ":")).encode() + b"\n")
    with open_ledger(store_dir) as scan:
        assert [r.capsule_id for r in scan.scan()] == [_cid("a"), _cid("crashed")]


def test_scan_verbs_keep_signed_admission(store_dir):
    result = _seal(store_dir)
    with open_ledger(store_dir) as scan:
        record = next(scan.scan())
        assert record.capsule_id == result.capsule["capsule_id"]
        assert record.authenticity == "signed"


def test_scan_verbs_work_on_a_read_only_copy_and_write_nothing(store_dir):
    _seal(store_dir)
    before = {p.relative_to(store_dir): p.read_bytes() for p in store_dir.rglob("*") if p.is_file()}
    for path in store_dir.rglob("*"):
        os.chmod(path, stat.S_IRUSR | (stat.S_IXUSR if path.is_dir() else 0))
    os.chmod(store_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with open_ledger(store_dir) as scan:
            assert len(list(scan.scan())) == 1
            assert {p.relative_to(store_dir) for p in store_dir.rglob("*") if p.is_file()} == set(before)
    finally:
        os.chmod(store_dir, stat.S_IRWXU)
        for path in store_dir.rglob("*"):
            os.chmod(path, stat.S_IRWXU)
    assert {p.relative_to(store_dir): p.read_bytes() for p in store_dir.rglob("*") if p.is_file()} == before



# -- review round 5 ------------------------------------------------------------


def test_scan_verbs_keep_a_false_flag_a_live_writer_left_in_the_wal(tmp_path, monkeypatch):
    root = tmp_path / "s"
    writer = LedgerStore(root, rotate_at_checkpoint=True)
    writer.append({"capsule_id": _cid("history")}, consequential=False)
    writer.append({"capsule_id": _cid("action")}, consequential=True)
    before = sorted(p.name for p in root.rglob("*"))
    try:
        monkeypatch.chdir(tmp_path)
        with open_ledger("s") as scan:
            assert [(r.capsule_id, r.consequential) for r in scan.scan()] == [
                (_cid("history"), False),
                (_cid("action"), True),
            ]
        assert sorted(p.name for p in root.rglob("*")) == before
    finally:
        writer.close()
