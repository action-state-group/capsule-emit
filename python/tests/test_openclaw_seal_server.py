# SPDX-License-Identifier: Apache-2.0
"""skills/openclaw/seal_server.py says so when an upgrade would split its
history: its old default was the flat capsule_ledger.jsonl, and it now writes
capsule-ledger/ (capsule-emit#358)."""
from __future__ import annotations

import importlib.util
import itertools
import warnings
from pathlib import Path

import pytest

from capsule_emit import ledger_migrate, seal
from capsule_emit.ledger_layout import LedgerDeprecationWarning

pytest.importorskip("fastapi")

_SERVER = Path(__file__).resolve().parents[2] / "skills" / "openclaw" / "seal_server.py"
_names = itertools.count()


def _load():
    spec = importlib.util.spec_from_file_location(f"openclaw_seal_server_{next(_names)}", _SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seal(path):
    return seal(None, action="act", operator="acme", anchor=False, witness=False, ledger=path)


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAPSULE_LEDGER", raising=False)


def test_startup_names_the_import_before_the_first_new_record(tmp_path):
    _seal(tmp_path / "capsule_ledger.jsonl")
    with pytest.warns(LedgerDeprecationWarning, match="capsule-emit ledger import capsule_ledger.jsonl capsule-ledger"):
        _load()


def test_the_first_record_in_a_second_history_warns(tmp_path):
    _seal(tmp_path / "capsule_ledger.jsonl")
    with pytest.warns(LedgerDeprecationWarning):
        server = _load()
    with pytest.warns(LedgerDeprecationWarning, match="history is in two ledgers"):
        server._warn_if_history_splits()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        server._warn_if_history_splits()  # once per process


def test_an_imported_old_ledger_is_one_history(tmp_path):
    _seal(tmp_path / "capsule_ledger.jsonl")
    ledger_migrate.import_ledger(tmp_path / "capsule_ledger.jsonl", tmp_path / "capsule-ledger")
    _seal(tmp_path / "capsule-ledger")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        server = _load()
        server._warn_if_history_splits()


def test_no_old_ledger_or_a_chosen_ledger_is_quiet(tmp_path, monkeypatch):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _load()._warn_if_history_splits()
    _seal(tmp_path / "capsule_ledger.jsonl")
    monkeypatch.setenv("CAPSULE_LEDGER", "capsule_ledger.jsonl")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _load()._warn_if_history_splits()


def test_an_empty_old_ledger_is_not_history(tmp_path):
    (tmp_path / "capsule_ledger.jsonl").touch()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _load()


def test_the_split_check_is_worked_out_once_the_new_ledger_exists(tmp_path, monkeypatch):
    _seal(tmp_path / "capsule_ledger.jsonl")
    ledger_migrate.import_ledger(tmp_path / "capsule_ledger.jsonl", tmp_path / "capsule-ledger")
    server = _load()
    calls = []
    real = server._first_id
    monkeypatch.setattr(server, "_first_id", lambda path: calls.append(path) or real(path))
    for _ in range(3):
        server._warn_if_history_splits()
    assert calls == []  # startup already worked it out; requests never reread a ledger
