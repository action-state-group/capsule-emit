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
    assert f"cp -n {flat}.signing_key.pem {tmp_path / 'capsule-ledger.signing_key.pem'}" in err
    assert not store.exists() and not (tmp_path / "capsule-ledger.signing_key.pem").exists()


def test_a_line_without_a_capsule_id_refuses_the_import(tmp_path):
    flat = tmp_path / "ledger.jsonl"
    flat.write_text(json.dumps({"capsule_id": "short"}) + "\n")
    with pytest.raises(ledger_migrate.ImportRefused, match="no 64-hex capsule_id") as info:
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
