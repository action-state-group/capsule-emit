# SPDX-License-Identifier: Apache-2.0
"""[emit-ledger-io-home] capsule_emit.ledger_io -- the read/verify seam
moved here from capsule-ledger's (archived) cli/ledger_io.py.

``open_ledger``/``require_ledger_path``/``add_scan_query_args``/
``build_scan_query``/``echo_parts`` use ``cll.ledger`` (already a hard
dependency) and are exercised unconditionally. ``local_payload_store``
needs the optional ``capsule-emit[ledger-io]`` extra (``capsule_ledger``,
Python >=3.10) and is skipped when that is not installed.
"""
from __future__ import annotations

import argparse

import pytest
from cll.ledger.store import LedgerStore

from capsule_emit import ledger_io, seal


@pytest.fixture
def sealed_capsules(tmp_path):
    scratch = tmp_path / "sealed-scratch.jsonl"
    return [
        seal(
            {"n": i},
            action=f"act-{i}",
            operator="acme",
            developer="agent@v1",
            anchor=False,
            ledger=scratch,
        ).capsule
        for i in range(3)
    ]


@pytest.fixture
def store_ledger(tmp_path, sealed_capsules):
    ledger_dir = tmp_path / "store"
    store = LedgerStore(root=ledger_dir, rotate_at_checkpoint=True)
    try:
        for cap in sealed_capsules:
            store.append(cap, consequential=False)
    finally:
        store.close()
    return ledger_dir, sealed_capsules


@pytest.fixture
def flat_ledger_file(tmp_path, sealed_capsules):
    import json

    path = tmp_path / "flat.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for cap in sealed_capsules:
            fh.write(json.dumps(cap, separators=(",", ":")) + "\n")
    return path, sealed_capsules


def _ids(records) -> list[str]:
    return [r.capsule["capsule_id"] if hasattr(r, "capsule") else r["capsule_id"] for r in records]


def test_open_ledger_over_a_store_directory_yields_the_real_store(store_ledger):
    ledger_dir, caps = store_ledger
    with ledger_io.open_ledger(ledger_dir) as store:
        scanned = list(store.scan())
        assert _ids(scanned) == [c["capsule_id"] for c in caps]


def test_open_ledger_over_a_jsonl_fixture_imports_into_a_throwaway_store(flat_ledger_file):
    path, caps = flat_ledger_file
    with ledger_io.open_ledger(path) as store:
        scanned = list(store.scan())
        assert _ids(scanned) == [c["capsule_id"] for c in caps]
    # the throwaway tempdir is cleaned up on exit -- nothing left rooted at path's dir
    assert not (path.parent / "does-not-linger").exists()


def test_local_payload_store_none_for_a_non_directory(tmp_path):
    missing = tmp_path / "not-a-real-path.jsonl"
    assert ledger_io.local_payload_store(missing) is None


def test_local_payload_store_none_for_a_directory_with_no_payload_store(tmp_path):
    pytest.importorskip("capsule_ledger")
    plain_dir = tmp_path / "store"
    plain_dir.mkdir()
    assert ledger_io.local_payload_store(plain_dir) is None


def test_local_payload_store_returns_a_store_once_populated(tmp_path):
    pytest.importorskip("capsule_ledger")
    from capsule_ledger.payload_store import PayloadStore

    ledger_dir = tmp_path / "store"
    ledger_dir.mkdir()
    PayloadStore(ledger_dir).put({"k": "v"})
    resolved = ledger_io.local_payload_store(ledger_dir)
    assert resolved is not None
    assert resolved.exists is True


def _args(**overrides):
    base = dict(ledger=None, agent=None, since=None, until=None, counterparty=None, verdict=None, action_type=None, limit=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_require_ledger_path_returns_the_flag_value():
    assert ledger_io.require_ledger_path("show", _args(ledger="/tmp/x")) == "/tmp/x"


def test_require_ledger_path_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("CAPSULE_LEDGER", "/tmp/from-env")
    assert ledger_io.require_ledger_path("show", _args()) == "/tmp/from-env"


def test_require_ledger_path_prints_a_usage_error_and_returns_none(monkeypatch, capsys):
    monkeypatch.delenv("CAPSULE_LEDGER", raising=False)
    result = ledger_io.require_ledger_path("show", _args())
    assert result is None
    err = capsys.readouterr().err
    assert "show" in err and "--ledger" in err


def test_add_scan_query_args_registers_every_filter_flag():
    parser = argparse.ArgumentParser()
    ledger_io.add_scan_query_args(parser)
    parsed = parser.parse_args(
        ["--ledger", "L", "--agent", "A", "--since", "S", "--until", "U", "--counterparty", "C", "--verdict", "V", "--action-type", "T", "--limit", "5"]
    )
    assert parsed.ledger == "L"
    assert parsed.agent == "A"
    assert parsed.since == "S"
    assert parsed.until == "U"
    assert parsed.counterparty == "C"
    assert parsed.verdict == "V"
    assert parsed.action_type == "T"
    assert parsed.limit == 5


def test_build_scan_query_maps_every_field():
    args = _args(agent="A", since="S", until="U", counterparty="C", verdict="V", action_type="T", limit=5)
    query = ledger_io.build_scan_query(args)
    assert query.agent == "A"
    assert query.since == "S"
    assert query.until == "U"
    assert query.counterparty == "C"
    assert query.verdict == "V"
    assert query.action_type == "T"
    assert query.limit == 5


def test_echo_parts_is_the_fixed_flag_order():
    args = _args(agent="A", since="S", until="U", counterparty="C", verdict="V", action_type="T", limit=5)
    assert ledger_io.echo_parts(args) == [
        ("--agent", "A"),
        ("--since", "S"),
        ("--until", "U"),
        ("--counterparty", "C"),
        ("--verdict", "V"),
        ("--action-type", "T"),
        ("--limit", 5),
    ]
