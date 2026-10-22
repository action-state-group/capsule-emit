# SPDX-License-Identifier: Apache-2.0
"""``capsule-emit ledger import`` and ``ledger export --jsonl``
(``capsule_emit.ledger_migrate``): a flat ledger moves into a ledger store
whole or not at all, keeps its leaves, checkpoints and producer key, and a
store writes back out as the same lines."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

import pytest
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import cli, ledger_migrate, ledger_store, seal
from capsule_emit.ledger import append_to_ledger, read_ledger_entries
from capsule_emit.legacy_ledger import LegacyLedger

_NO_WITNESS = "http://127.0.0.1:1"


def _cid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _seal(path, action="act", **kwargs):
    return seal(None, action=action, operator="acme", anchor=False, ledger=path, witness_url=_NO_WITNESS, **kwargs)


@pytest.fixture
def witnessed_flat(tmp_path, monkeypatch):
    url, _, stop = _start_stub_ts()
    monkeypatch.setenv("CAPSULE_WITNESS_URL", url)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    path = tmp_path / "ledger.jsonl"
    for action in ("c0", "c1"):
        seal(None, action=action, operator="acme", anchor=False, ledger=path, witness_url=url)
    assert _wait_for(lambda: _stamp_count(path) >= 1)
    yield path
    stop()


def test_import_keeps_every_line_leaf_and_checkpoint(witnessed_flat, tmp_path, capsys):
    store = tmp_path / "capsule-ledger"
    assert cli.main(["ledger", "import", str(witnessed_flat), str(store)]) == 0
    out = capsys.readouterr().out
    assert "imported 3 line(s)" in out and "1 checkpoint_stamp" in out and "2 capsule" in out
    assert read_ledger_entries(store) == read_ledger_entries(witnessed_flat)
    flat, imported = LegacyLedger(witnessed_flat), LegacyLedger(store)
    assert [e.value for e in imported.entries()] == [e.value for e in flat.entries()]
    assert imported.load_cll().checkpoint == flat.load_cll().checkpoint
    assert (store / ledger_store.MARKER_FILENAME).is_file()


def test_export_of_an_import_is_the_source_byte_for_byte(witnessed_flat, tmp_path, capsys):
    store = tmp_path / "capsule-ledger"
    ledger_migrate.import_ledger(witnessed_flat, store)
    out = tmp_path / "copy.jsonl"
    assert cli.main(["ledger", "export", "--jsonl", str(store), "-o", str(out)]) == 0
    assert out.read_bytes() == witnessed_flat.read_bytes()
    again = tmp_path / "again"
    ledger_migrate.import_ledger(out, again)
    assert read_ledger_entries(again) == read_ledger_entries(store)


def test_export_to_stdout(tmp_path, capsysbinary):
    store = tmp_path / "s"
    store.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, store)
    assert cli.main(["ledger", "export", "--jsonl", str(store)]) == 0
    assert capsysbinary.readouterr().out == json.dumps({"capsule_id": _cid("a")}, separators=(",", ":")).encode() + b"\n"


def test_the_producer_key_moves_with_the_ledger(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    first = _seal(flat).capsule
    store = tmp_path / "capsule-ledger"
    report = ledger_migrate.import_ledger(flat, store)
    assert report.key_copied == tmp_path / "capsule-ledger.signing_key.pem"
    assert stat.S_IMODE(os.stat(report.key_copied).st_mode) == 0o600
    assert _seal(store).capsule["key_id"] == first["key_id"]


def test_an_existing_store_key_is_never_overwritten(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    key = tmp_path / "capsule-ledger.signing_key.pem"
    key.write_bytes(b"existing")
    report = ledger_migrate.import_ledger(flat, tmp_path / "capsule-ledger")
    assert report.key_copied is None and key.read_bytes() == b"existing"


@pytest.mark.parametrize("second", [{"n": 1}, {"n": 2}])
def test_a_repeated_capsule_id_refuses_the_import(tmp_path, capsys, second):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    with open(flat, "a") as fh:
        fh.write(json.dumps({"capsule_id": _cid("x"), "n": 1}) + "\n")
        fh.write(json.dumps({"capsule_id": _cid("x"), **second}) + "\n")
    store = tmp_path / "capsule-ledger"
    assert cli.main(["ledger", "import", str(flat), str(store)]) == 1
    err = capsys.readouterr().err
    assert "line(s) 3 repeat a capsule_id" in err and "repeated hold lines" in err
    assert f"cp -n -- {flat}.signing_key.pem {tmp_path / 'capsule-ledger.signing_key.pem'}" in err
    assert not store.exists() and not (tmp_path / "capsule-ledger.signing_key.pem").exists()


def test_a_line_without_a_capsule_id_refuses_the_import(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": "short"}) + "\n")
    with pytest.raises(ledger_migrate.ImportRefused, match="no lowercase 64-hex capsule_id") as info:
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    assert info.value.lines == [1]
    assert not (tmp_path / "s").exists()


def test_an_unparseable_fragment_is_skipped_and_reported(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    flat.write_bytes(
        json.dumps({"capsule_id": _cid("a")}).encode() + b"\n" + b'{"capsule_id":"torn\n'
        + json.dumps({"capsule_id": _cid("b")}).encode() + b"\n"
    )
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "skipped line(s) 2: not JSON" in capsys.readouterr().out
    assert [e.value for e in LegacyLedger(tmp_path / "s").entries()] == [e.value for e in LegacyLedger(flat).entries()]


@pytest.mark.parametrize("make", ["file", "full_dir", "dotted"])
def test_the_destination_must_be_a_new_store(tmp_path, make):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    dest = tmp_path / ("audit.store" if make == "dotted" else "s")
    if make == "file":
        dest.write_text("x")
    elif make == "full_dir":
        dest.mkdir()
        (dest / "x").write_text("x")
    with pytest.raises(ledger_migrate.ImportRefused):
        ledger_migrate.import_ledger(flat, dest)


def test_an_empty_directory_is_a_valid_destination(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    dest = tmp_path / "s"
    dest.mkdir()
    ledger_migrate.import_ledger(flat, dest)
    assert len(read_ledger_entries(dest)) == 1


def test_an_interrupted_import_publishes_nothing(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    from cll.ledger import LedgerStore

    def boom(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(LedgerStore, "append", boom)
    with pytest.raises(KeyboardInterrupt):
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    monkeypatch.undo()
    assert not (tmp_path / "s").exists()
    assert [p.name for p in tmp_path.iterdir() if ".staging-" in p.name] == []
    ledger_migrate.import_ledger(flat, tmp_path / "s")


def test_export_refuses_a_directory_that_is_not_a_store(tmp_path, capsys):
    assert cli.main(["ledger", "export", "--jsonl", str(tmp_path), "-o", str(tmp_path / "x.jsonl")]) == 1
    assert not (tmp_path / "x.jsonl").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("audit.log", "audit-log-store"),
        ("v1.2.jsonl", "v1-2-jsonl-store"),
        (".audit.jsonl", "audit-jsonl-store"),
        ("audit", "audit-store"),
    ],
)
def test_suggested_store_names(tmp_path, name, expected):
    assert ledger_migrate.suggested_store(tmp_path / name) == os.fspath(tmp_path / expected)


def test_suggested_store_for_the_default_ledger_and_collisions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ledger_migrate.suggested_store("ledger.jsonl") == os.path.join(".", "capsule-ledger")
    (tmp_path / "capsule-ledger").mkdir()
    (tmp_path / "capsule-ledger" / "x").write_text("x")
    assert ledger_migrate.suggested_store("ledger.jsonl") == os.path.join(".", "capsule-ledger-2")
    (tmp_path / "audit-log-store").write_text("taken")
    assert ledger_migrate.suggested_store("audit.log") == os.path.join(".", "audit-log-store-2")


def test_the_suggested_store_is_accepted_by_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("ledger.jsonl", "audit.log", ".hidden.jsonl"):
        _seal(tmp_path / name)
        target = ledger_migrate.suggested_store(name)
        assert not target.startswith("-")
        ledger_migrate.import_ledger(name, target)
        assert read_ledger_entries(target)


def test_export_reports_repeats_it_skipped(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    manifest = json.loads((root / "manifest.json").read_text())
    with open(root / "segments" / manifest["active_segment"], "ab") as fh:
        fh.write(json.dumps({"capsule_id": _cid("a")}, separators=(",", ":")).encode() + b"\n")
    out = io.BytesIO()
    report = ledger_migrate.export_ledger(root, out)
    assert report.lines == 1 and report.skipped_repeats == 1


# -- review round 1 ------------------------------------------------------------


def test_a_key_appearing_during_import_is_never_replaced(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    dest_key = tmp_path / "capsule-ledger.signing_key.pem"
    real_link = os.link

    def racing_link(src, dst):
        dest_key.write_bytes(b"a key a concurrent seal() made")
        return real_link(src, dst)

    monkeypatch.setattr(ledger_migrate.os, "link", racing_link)
    with pytest.raises(ledger_migrate.ImportRefused, match="appeared"):
        ledger_migrate.import_ledger(flat, tmp_path / "capsule-ledger")
    assert dest_key.read_bytes() == b"a key a concurrent seal() made"
    assert not (tmp_path / "capsule-ledger").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".capsule-ledger.signing_key")] == []


def test_an_interrupted_key_copy_leaves_no_partial_key_and_a_retry_works(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    first = _seal(flat).capsule
    real_fsync = os.fsync

    def fail(fd):
        raise OSError("I/O error")

    monkeypatch.setattr(ledger_migrate.os, "fsync", fail)
    with pytest.raises(OSError):
        ledger_migrate.import_ledger(flat, tmp_path / "capsule-ledger")
    monkeypatch.setattr(ledger_migrate.os, "fsync", real_fsync)
    assert not (tmp_path / "capsule-ledger.signing_key.pem").exists()
    ledger_migrate.import_ledger(flat, tmp_path / "capsule-ledger")
    assert _seal(tmp_path / "capsule-ledger").capsule["key_id"] == first["key_id"]


def test_a_failed_rename_publishes_no_store_and_keeps_the_key(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)

    def fail(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(ledger_store.os, "rename", fail)
    with pytest.raises(OSError):
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    monkeypatch.undo()
    assert not (tmp_path / "s").exists()
    assert (tmp_path / "s.signing_key.pem").is_file()
    assert [p.name for p in tmp_path.iterdir() if ".staging-" in p.name] == []
    ledger_migrate.import_ledger(flat, tmp_path / "s")


def test_a_failure_after_the_rename_leaves_a_complete_store(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    real = ledger_store._fsync
    target_parent = os.fspath(tmp_path)

    def fail_parent(path):
        # Only the fsync of the parent after the store was renamed into place.
        if os.fspath(path) == target_parent and (tmp_path / "s").exists():
            raise OSError("parent fsync failed")
        return real(path)

    monkeypatch.setattr(ledger_store, "_fsync", fail_parent)
    with pytest.raises(OSError):
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    monkeypatch.undo()
    assert (tmp_path / "s" / ledger_store.MARKER_FILENAME).is_file()
    assert read_ledger_entries(tmp_path / "s") == read_ledger_entries(flat)


def test_a_stale_staging_directory_is_left_alone(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    stale = tmp_path / ".s.staging-deadbeef"
    stale.mkdir()
    ledger_migrate.import_ledger(flat, tmp_path / "s")
    assert stale.is_dir() and read_ledger_entries(tmp_path / "s")


def test_a_line_the_store_cannot_hold_refuses_the_import(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": _cid("a"), "timestamp": 10**30}) + "\n")
    with pytest.raises(ledger_migrate.ImportRefused, match="cannot be stored"):
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    assert not (tmp_path / "s").exists()


def test_reserialized_lines_are_reported(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": _cid("a"), "n": 1}, indent=None) + "\n")
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "line(s) 1: stored in compact JSON form" in capsys.readouterr().out


def test_invalid_utf8_inside_a_line_is_kept_as_the_flat_reader_keeps_it(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    flat.write_bytes(b'{"capsule_id":"' + _cid("a").encode() + b'","note":"\xff"}\n' + json.dumps({"capsule_id": _cid("b")}).encode() + b"\n")
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "not UTF-8, stored as U+FFFD" in capsys.readouterr().out
    assert [e.value for e in LegacyLedger(tmp_path / "s").entries()] == [e.value for e in LegacyLedger(flat).entries()]


def test_export_refuses_a_directory_target_and_one_inside_the_store(tmp_path, capsys):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert cli.main(["ledger", "export", "--jsonl", str(root), "-o", str(out_dir)]) == 1
    assert list(out_dir.iterdir()) == []
    assert cli.main(["ledger", "export", "--jsonl", str(root), "-o", str(root / "manifest.json")]) == 1
    assert json.loads((root / "manifest.json").read_text())["segments"]


def test_export_file_has_the_usual_mode(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    old = os.umask(0o022)
    try:
        ledger_migrate.export_to_file(root, tmp_path / "x.jsonl")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "x.jsonl").st_mode) == 0o644


def test_export_refuses_an_archived_store(tmp_path):
    from cll.checkpoint.index import MmrLedger
    from cll.ledger import LedgerStore
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
    with pytest.raises(ledger_store.LedgerArchivedError):
        ledger_migrate.export_ledger(root, io.BytesIO())


def test_the_key_report_names_an_override_and_a_missing_key(tmp_path, capsys, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": _cid("a")}) + "\n")
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "creates a new one" in capsys.readouterr().out
    monkeypatch.setenv("CAPSULE_SIGNING_KEY_PATH", str(tmp_path / "kms.pem"))
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "t")]) == 0
    assert "the key CAPSULE_SIGNING_KEY_PATH names" in capsys.readouterr().out


def test_the_copy_command_is_shell_safe(tmp_path, capsys):
    odd = tmp_path / "my dir"
    odd.mkdir()
    flat = odd / "ledger.jsonl"
    _seal(flat)
    with open(flat, "a") as fh:
        line = json.dumps({"capsule_id": _cid("x")})
        fh.write(line + "\n" + line + "\n")
    with pytest.raises(ledger_migrate.ImportRefused) as info:
        ledger_migrate.import_ledger(flat, odd / "s")
    import shlex

    command = str(info.value).split("new directory: ", 1)[1]
    assert shlex.split(command)[:3] == ["cp", "-n", "--"]
    assert shlex.split(command)[3] == f"{flat}.signing_key.pem"


def test_a_missing_parent_is_refused_clearly(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    with pytest.raises(ledger_migrate.ImportRefused, match="does not exist"):
        ledger_migrate.import_ledger(flat, tmp_path / "nope" / "s")


def test_suggested_store_skips_a_name_whose_key_is_left_over(tmp_path):
    (tmp_path / "audit-log-store.signing_key.pem").write_text("left over")
    assert ledger_migrate.suggested_store(tmp_path / "audit.log") == os.fspath(tmp_path / "audit-log-store-2")


def test_a_new_store_directory_gets_the_umask_default(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    old = os.umask(0o027)
    try:
        ledger_migrate.import_ledger(flat, tmp_path / "s")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "s").stat().st_mode) == 0o750


def test_a_first_seal_into_an_empty_directory_is_consequential(tmp_path):
    import sqlite3

    root = tmp_path / "s"
    root.mkdir()
    _seal(root)
    _seal(root)
    with sqlite3.connect(root / "index.sqlite3") as conn:
        assert [r[0] for r in conn.execute("SELECT consequential FROM records ORDER BY seq")] == [1, 1]


# -- review round 2 ------------------------------------------------------------


def test_a_new_default_key_never_replaces_one_that_appeared(tmp_path, monkeypatch):
    from capsule_emit import signing

    key = tmp_path / "s.signing_key.pem"
    other = signing.LocalKeypairSigner(tmp_path / "other.pem")
    real_link = os.link

    def racing_link(src, dst):
        # import (or another process) publishes its key first
        real_link(tmp_path / "other.pem", dst)
        return real_link(src, dst)

    monkeypatch.setattr(signing.os, "link", racing_link)
    signer = signing.LocalKeypairSigner(key)
    assert signer.key_id == other.key_id
    assert key.read_bytes() == (tmp_path / "other.pem").read_bytes()


@pytest.mark.parametrize("sidecar", ["s.signing_key.pem", "s.lock"])
def test_export_refuses_the_stores_key_and_lock(tmp_path, sidecar):
    root = tmp_path / "s"
    root.mkdir()
    _seal(root)
    target = tmp_path / sidecar
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(ledger_store.LedgerLayoutError, match="part of the store"):
        ledger_migrate.export_to_file(root, target)
    assert (target.read_bytes() if target.exists() else None) == before


def test_a_line_wrapped_in_unicode_whitespace_is_kept(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    flat.write_bytes(
        json.dumps({"capsule_id": _cid("a")}).encode() + b"\n"
        + "\u00a0".encode() + json.dumps({"capsule_id": _cid("b")}).encode() + "\u00a0".encode() + b"\n"
    )
    ledger_migrate.import_ledger(flat, tmp_path / "s")
    assert [e.value for e in LegacyLedger(tmp_path / "s").entries()] == [e.value for e in LegacyLedger(flat).entries()]


def test_a_store_key_that_differs_from_the_old_one_is_reported(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    from capsule_emit import signing

    signing.LocalKeypairSigner(tmp_path / "s.signing_key.pem")
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "is not the old ledger's key" in capsys.readouterr().out


def test_an_uppercase_capsule_id_is_refused_with_the_reason(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": _cid("a").upper()}) + "\n")
    with pytest.raises(ledger_migrate.ImportRefused, match="lowercase 64-hex"):
        ledger_migrate.import_ledger(flat, tmp_path / "s")


def test_a_dangling_symlink_destination_is_refused_clearly(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    (tmp_path / "link").symlink_to(tmp_path / "missing" / "s")
    with pytest.raises(ledger_migrate.ImportRefused, match="does not exist"):
        ledger_migrate.import_ledger(flat, tmp_path / "link")


def test_a_suggested_name_under_a_dash_directory_stays_a_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-runs").mkdir()
    suggestion = ledger_migrate.suggested_store("-runs/audit.jsonl")
    assert suggestion.startswith("./") and not suggestion.startswith("-")


def test_reimporting_an_export_keeps_the_checkpoints(witnessed_flat, tmp_path):
    first = tmp_path / "first"
    ledger_migrate.import_ledger(witnessed_flat, first)
    out = tmp_path / "copy.jsonl"
    ledger_migrate.export_to_file(first, out)
    second = tmp_path / "second"
    ledger_migrate.import_ledger(out, second)
    assert LegacyLedger(second).load_cll().checkpoint == LegacyLedger(witnessed_flat).load_cll().checkpoint


# -- review round 3 ------------------------------------------------------------


def test_the_differing_key_note_names_the_store_key_under_an_override(tmp_path, capsys, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    _seal(flat)
    from capsule_emit import signing

    signing.LocalKeypairSigner(tmp_path / "s.signing_key.pem")
    monkeypatch.setenv("CAPSULE_SIGNING_KEY_PATH", str(tmp_path / "kms.pem"))
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert f"note: {tmp_path / 's.signing_key.pem'} was already there" in out
    assert "while CAPSULE_SIGNING_KEY_PATH is set" in out


def test_a_line_with_a_real_replacement_char_and_a_bad_byte_is_reported(tmp_path, capsys):
    flat = tmp_path / "ledger.jsonl"
    flat.write_bytes(b'{"capsule_id":"' + _cid("a").encode() + b'","x":"\xef\xbf\xbd \xff"}\n')
    assert cli.main(["ledger", "import", str(flat), str(tmp_path / "s")]) == 0
    assert "line(s) 1: held bytes that are not UTF-8" in capsys.readouterr().out


@pytest.mark.parametrize("alias", ["name", "symlink", "hardlink"])
def test_a_source_that_is_the_stores_lock_file_is_refused_untouched(tmp_path, alias):
    real = tmp_path / "ledger.jsonl"
    _seal(real)
    lock = tmp_path / "s.lock"
    if alias == "name":
        real.rename(lock)
        source = lock
    elif alias == "symlink":
        lock.symlink_to(real)
        source = real
    else:
        os.link(real, lock)
        source = real
    before = (lock if alias == "name" else real).read_bytes()
    with pytest.raises(ledger_migrate.ImportRefused, match="lock file"):
        ledger_migrate.import_ledger(source, tmp_path / "s")
    assert (lock if alias == "name" else real).read_bytes() == before


@pytest.mark.parametrize("sidecar", ["s.signing_key.pem", "s.lock"])
def test_export_refuses_a_symlinked_key_or_lock(tmp_path, sidecar):
    root = tmp_path / "s"
    root.mkdir()
    _seal(root)
    elsewhere = tmp_path / f"elsewhere-{sidecar}"
    target = tmp_path / sidecar
    if target.exists():
        target.rename(elsewhere)
    else:
        elsewhere.write_text("lock")
    target.symlink_to(elsewhere)
    with pytest.raises(ledger_store.LedgerLayoutError, match="part of the store"):
        ledger_migrate.export_to_file(root, target)
    assert target.is_symlink()



# -- review round 4 ------------------------------------------------------------


@pytest.mark.parametrize("dangling", [False, True])
def test_export_refuses_an_absent_or_dangling_key_name(tmp_path, dangling):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    key = tmp_path / "s.signing_key.pem"
    assert not key.exists()
    if dangling:
        key.symlink_to(tmp_path / "nowhere.pem")
    with pytest.raises(ledger_store.LedgerLayoutError, match="part of the store"):
        ledger_migrate.export_to_file(root, key)
    assert key.is_symlink() == dangling and not key.exists()


# -- review round 5 ------------------------------------------------------------


def test_export_never_replaces_an_existing_file(tmp_path, capsys):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    target = tmp_path / "copy.jsonl"
    target.write_text("keep me")
    assert cli.main(["ledger", "export", "--jsonl", str(root), "-o", str(target)]) == 1
    assert target.read_text() == "keep me"


def test_export_refuses_a_case_variant_of_the_key_on_a_case_insensitive_disk(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    _seal(root)
    key = tmp_path / "s.signing_key.pem"
    variant = tmp_path / "S.SIGNING_KEY.PEM"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    before = key.read_bytes()
    with pytest.raises(ledger_store.LedgerLayoutError):
        ledger_migrate.export_to_file(root, variant)
    assert key.read_bytes() == before


def test_an_export_that_meets_a_new_file_at_its_name_does_not_replace_it(tmp_path, monkeypatch):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    target = tmp_path / "copy.jsonl"
    real_link = os.link

    def racing_link(src, dst):
        target.write_text("created meanwhile")
        return real_link(src, dst)

    monkeypatch.setattr(ledger_migrate.os, "link", racing_link)
    with pytest.raises(ledger_store.LedgerLayoutError, match="appeared during the export"):
        ledger_migrate.export_to_file(root, target)
    assert target.read_text() == "created meanwhile"


# -- review round 6 ------------------------------------------------------------


def test_export_refuses_where_an_absent_key_symlink_points(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    (tmp_path / "s.signing_key.pem").symlink_to(tmp_path / "elsewhere.pem")
    with pytest.raises(ledger_store.LedgerLayoutError, match="part of the store"):
        ledger_migrate.export_to_file(root, tmp_path / "elsewhere.pem")
    assert not (tmp_path / "elsewhere.pem").exists()


def test_export_refuses_a_case_variant_of_an_absent_key(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)
    with pytest.raises(ledger_store.LedgerLayoutError, match="part of the store"):
        ledger_migrate.export_to_file(root, tmp_path / "S.SIGNING_KEY.PEM")


def test_export_works_without_hard_links(tmp_path, monkeypatch):
    root = tmp_path / "s"
    root.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, root)

    def no_links(src, dst):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(ledger_migrate.os, "link", no_links)
    ledger_migrate.export_to_file(root, tmp_path / "copy.jsonl")
    assert json.loads((tmp_path / "copy.jsonl").read_text())["capsule_id"] == _cid("a")


# -- the stand-in's delta check of 3a112c6 -------------------------------------------


def test_export_refuses_the_store_directory_in_another_case(tmp_path):
    store = tmp_path / "s"
    store.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, store)
    if not (tmp_path / "S").exists():
        pytest.skip("needs a case-insensitive disk")
    with pytest.raises(ledger_store.LedgerLayoutError, match="is part of the store"):
        ledger_migrate.export_to_file(store, tmp_path / "S" / "out.jsonl")
    assert not (store / "out.jsonl").exists()


def test_export_tolerates_a_disk_that_cannot_fsync_a_directory(tmp_path, monkeypatch):
    import errno

    store = tmp_path / "s"
    store.mkdir()
    append_to_ledger({"capsule_id": _cid("a")}, store)
    real = ledger_store._fsync

    def no_dir_fsync(path):
        if os.fspath(path) == os.fspath(tmp_path):  # the export's parent directory
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(path)

    monkeypatch.setattr(ledger_store, "_fsync", no_dir_fsync)
    ledger_migrate.export_to_file(store, tmp_path / "out.jsonl")
    assert (tmp_path / "out.jsonl").read_bytes()


# -- Codex round 7: disks without hard links ----------------------------------------


def _no_hard_links(monkeypatch):
    import errno

    def refuse(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", refuse)


def test_a_new_producer_key_is_created_without_hard_links(tmp_path, monkeypatch):
    _no_hard_links(monkeypatch)
    result = _seal(tmp_path / "s")
    key = tmp_path / "s.signing_key.pem"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    from capsule_emit.signing import LocalKeypairSigner

    assert LocalKeypairSigner(key).key_id == result.capsule["key_id"]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_import_copies_the_key_without_hard_links(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    first = _seal(flat)
    _no_hard_links(monkeypatch)
    ledger_migrate.import_ledger(flat, tmp_path / "s")
    assert (tmp_path / "s.signing_key.pem").read_bytes() == (tmp_path / "ledger.jsonl.signing_key.pem").read_bytes()
    assert _seal(tmp_path / "s").capsule["key_id"] == first.capsule["key_id"]


def test_publish_new_file_never_replaces_with_or_without_hard_links(tmp_path, monkeypatch):
    target = tmp_path / "k.pem"
    target.write_bytes(b"theirs")
    assert ledger_store.publish_new_file(target, b"ours") is False
    _no_hard_links(monkeypatch)
    assert ledger_store.publish_new_file(target, b"ours") is False
    assert target.read_bytes() == b"theirs"
    assert ledger_store.publish_new_file(tmp_path / "new.pem", b"ours") is True
    assert (tmp_path / "new.pem").read_bytes() == b"ours"


def test_without_hard_links_a_key_appears_only_complete(tmp_path, monkeypatch):
    _no_hard_links(monkeypatch)
    target = tmp_path / "k.pem"
    seen = []
    real_rename = os.rename

    def rename(src, dst):
        seen.append((Path(src).read_bytes(), os.path.lexists(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    assert ledger_store.publish_new_file(target, b"complete key") is True
    assert seen == [(b"complete key", False)]  # moved into place whole, never written there
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
