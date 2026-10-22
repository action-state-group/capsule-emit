# SPDX-License-Identifier: Apache-2.0
"""Which ledger a call writes, and the deprecation of the flat ledger.jsonl
(``capsule_emit.ledger_layout``, capsule-emit#358): the store is the default,
an existing flat ledger keeps working with a warning until FLAT_WRITES_STOP,
and no new flat ledger is created."""
from __future__ import annotations

import json
import threading
import warnings

import pytest

from capsule_emit import ledger_layout, ledger_migrate, ledger_store, push, seal
from capsule_emit.ledger import read_ledger, read_ledger_entries
from capsule_emit.ledger_layout import FlatLedgerWriteRefused, LedgerDeprecationWarning, resolve_ledger

_NO_WITNESS = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _the_real_rules(monkeypatch, tmp_path):
    # The suite's conftest relaxes these for its flat-ledger tests; here they
    # are what is under test.
    monkeypatch.setattr(ledger_layout, "_allow_new_flat", False)
    monkeypatch.setattr(ledger_layout, "_warn_flat_writes", True)
    monkeypatch.setattr(ledger_layout, "_warned", set())
    monkeypatch.delenv("CAPSULE_LEDGER", raising=False)
    monkeypatch.chdir(tmp_path)


def _seal(**kwargs):
    return seal(None, action="act", operator="acme", anchor=False, witness_url=_NO_WITNESS, **kwargs)


# -- the default ---------------------------------------------------------------


def test_a_fresh_seal_writes_the_store(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = _seal()
    assert (tmp_path / "capsule-ledger" / "manifest.json").is_file()
    assert (tmp_path / "capsule-ledger" / ledger_store.MARKER_FILENAME).is_file()
    assert read_ledger(tmp_path / "capsule-ledger") == [result.capsule]
    assert not (tmp_path / "ledger.jsonl").exists()
    assert (tmp_path / "capsule-ledger.signing_key.pem").is_file()


def test_an_existing_flat_default_keeps_working_with_one_warning(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    with pytest.warns(LedgerDeprecationWarning) as caught:
        _seal()
        _seal()
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "capsule-emit ledger import ledger.jsonl ./capsule-ledger" in message
    assert 'ledger="./capsule-ledger" (or leave ledger= unset to use the default)' in message
    assert f"stop in capsule-emit {ledger_layout.FLAT_WRITES_STOP}" in message
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 2
    assert not (tmp_path / "capsule-ledger").exists()


def test_an_existing_store_wins_over_the_flat_default(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    (tmp_path / "capsule-ledger").mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _seal()
    assert len(read_ledger(tmp_path / "capsule-ledger")) == 1


def test_a_default_directory_that_is_not_a_store_is_refused(tmp_path):
    (tmp_path / "capsule-ledger").mkdir()
    (tmp_path / "capsule-ledger" / "notes.txt").write_text("x")
    with pytest.raises(ledger_store.LedgerLayoutError):
        _seal()


def test_capsule_ledger_env_is_the_default_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_LEDGER", str(tmp_path / "from-env"))
    _seal()
    assert len(read_ledger(tmp_path / "from-env")) == 1
    assert not (tmp_path / "capsule-ledger").exists()


def test_an_env_flat_ledger_warns_with_the_env_remedy(tmp_path, monkeypatch):
    (tmp_path / "audit.jsonl").touch()
    monkeypatch.setenv("CAPSULE_LEDGER", str(tmp_path / "audit.jsonl"))
    with pytest.warns(LedgerDeprecationWarning, match=r", then set CAPSULE_LEDGER=.*audit-jsonl-store\. "):
        _seal()


# -- explicit paths ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "expected"),
    [("store_name", "store"), ("empty_dir", "store"), ("existing_file", "flat")],
)
def test_an_explicit_path_resolves_by_what_it_is(tmp_path, make, expected):
    target = tmp_path / ("audit.jsonl" if make == "existing_file" else "audit")
    if make == "empty_dir":
        target.mkdir()
    elif make == "existing_file":
        target.touch()
    assert resolve_ledger(target).layout == expected


def test_no_new_flat_ledger_is_created(tmp_path):
    with pytest.raises(FlatLedgerWriteRefused, match="no longer creates flat ledgers") as info:
        _seal(ledger=tmp_path / "new.jsonl")
    assert "new-jsonl-store" in str(info.value)
    assert not (tmp_path / "new.jsonl").exists()


@pytest.mark.parametrize("name", ["audit.log", "runs.v1"])
def test_a_new_name_with_a_dot_is_refused(tmp_path, name):
    with pytest.raises(ledger_store.LedgerLayoutError, match="no '.'"):
        _seal(ledger=tmp_path / name)


def test_an_unrelated_directory_is_refused(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "x").write_text("x")
    with pytest.raises(ledger_store.LedgerLayoutError):
        _seal(ledger=d)


# -- the end of the window ---------------------------------------------------------


def test_from_flat_writes_stop_a_new_record_to_a_flat_ledger_is_refused(tmp_path, monkeypatch):
    (tmp_path / "ledger.jsonl").touch()
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    with pytest.raises(FlatLedgerWriteRefused, match="are refused from capsule-emit 0.15.0") as info:
        _seal()
    assert "capsule-emit ledger import ledger.jsonl ./capsule-ledger" in str(info.value)
    assert read_ledger_entries(tmp_path / "ledger.jsonl") == []


def test_checkpoint_stamps_on_a_flat_ledger_never_stop(tmp_path, monkeypatch):
    (tmp_path / "ledger.jsonl").touch()
    _seal()
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        push(witness=False)


def test_holds_on_a_flat_ledger_follow_the_window(tmp_path, monkeypatch):
    from capsule_emit.holds import Action, HoldEngine

    (tmp_path / "ledger.jsonl").touch()
    engine = HoldEngine(ledger_path=tmp_path / "ledger.jsonl", cap_minor={"money.transfer": 100})
    action = Action(verb="money.transfer", operator="acme", developer="d", amount_minor=10, currency="USD")
    with pytest.warns(LedgerDeprecationWarning):
        engine.evaluate_and_reserve(action)
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    with pytest.raises(FlatLedgerWriteRefused):
        engine.evaluate_and_reserve(action)


# -- D3 (b): the default moves while a call waits ---------------------------------------


def test_a_seal_that_waited_while_an_import_published_the_store_goes_to_the_store(tmp_path, monkeypatch):
    from capsule_emit.legacy_ledger import LegacyLedger

    (tmp_path / "ledger.jsonl").write_text("")
    _seal()  # a first record in the old default
    real_write = LegacyLedger._write
    imported = threading.Event()

    def write_after_an_import(self, line, **kwargs):
        if kwargs.get("precheck") is not None and not imported.is_set():
            imported.set()
            ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
        return real_write(self, line, **kwargs)

    monkeypatch.setattr(LegacyLedger, "_write", write_after_an_import)
    second = _seal()
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1
    store = read_ledger(tmp_path / "capsule-ledger")
    assert [c["capsule_id"] for c in store][-1] == second.capsule["capsule_id"]
    assert len(store) == 2


def test_a_seal_to_the_old_flat_file_after_import_is_by_choice_only(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    _seal()
    ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
    _seal()
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1
    assert len(read_ledger(tmp_path / "capsule-ledger")) == 2
    json.loads((tmp_path / "capsule-ledger" / "manifest.json").read_text())



def test_append_to_ledger_creates_no_new_flat_file(tmp_path):
    from capsule_emit.ledger import append_to_ledger

    with pytest.raises(FlatLedgerWriteRefused):
        append_to_ledger({"capsule_id": "ab" * 32}, tmp_path / "new.jsonl")
    assert not (tmp_path / "new.jsonl").exists()


def test_legacy_ledger_init_creates_no_new_flat_file(tmp_path):
    from capsule_emit.legacy_ledger import LegacyLedger

    with pytest.raises(FlatLedgerWriteRefused):
        LegacyLedger(tmp_path / "new.jsonl").init()
    assert not (tmp_path / "new.jsonl").exists()


def test_a_new_jsonl_path_from_the_env_names_the_env_remedy(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_LEDGER", str(tmp_path / "new.jsonl"))
    with pytest.raises(FlatLedgerWriteRefused, match="set CAPSULE_LEDGER="):
        _seal()
