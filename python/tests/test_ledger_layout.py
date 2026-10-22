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


# -- D3 (b): what follows the store when the call runs again ------------------------


def _import_during_the_first_precheck(monkeypatch, tmp_path, during=None):
    """Patch LegacyLedger._write so that, the first time a default-flat write
    reaches its precheck, ``during`` runs first (by default, the import that
    publishes ./capsule-ledger/)."""
    from capsule_emit.legacy_ledger import LegacyLedger

    real_write = LegacyLedger._write
    fired = threading.Event()

    def write(self, line, **kwargs):
        if kwargs.get("precheck") is not None and not fired.is_set():
            fired.set()
            if during is None:
                ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
            else:
                during()
        return real_write(self, line, **kwargs)

    monkeypatch.setattr(LegacyLedger, "_write", write)
    return fired


def test_a_rerun_seal_keeps_its_confirms_link(tmp_path, monkeypatch):
    (tmp_path / "ledger.jsonl").touch()
    first = _seal()
    _import_during_the_first_precheck(monkeypatch, tmp_path)
    second = _seal(confirms=first.capsule["capsule_id"])
    store = read_ledger(tmp_path / "capsule-ledger")
    assert [c["capsule_id"] for c in store] == [first.capsule["capsule_id"], second.capsule["capsule_id"]]
    assert store[1]["chain"] == {"parent_capsule_id": first.capsule["capsule_id"], "relation": "confirms"}


def test_a_rerun_seal_with_require_witness_reports_the_store(tmp_path, monkeypatch):
    from capsule_emit.witness import WitnessRequiredError

    (tmp_path / "ledger.jsonl").touch()
    _seal()
    _import_during_the_first_precheck(monkeypatch, tmp_path)
    with pytest.raises(WitnessRequiredError, match="capsule-ledger"):
        _seal(require_witness=True)
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1
    assert len(read_ledger(tmp_path / "capsule-ledger")) == 2


def test_a_rerun_seal_signs_with_the_stores_key(tmp_path, monkeypatch):
    (tmp_path / "ledger.jsonl").touch()
    flat_key = _seal().capsule["key_id"]
    made = {}

    def another_process_makes_the_store():
        made["key"] = _seal(ledger="capsule-ledger").capsule["key_id"]

    _import_during_the_first_precheck(monkeypatch, tmp_path, during=another_process_makes_the_store)
    rerun = _seal()
    assert made["key"] != flat_key
    assert rerun.capsule["key_id"] == made["key"]
    assert [c["key_id"] for c in read_ledger(tmp_path / "capsule-ledger")] == [made["key"]] * 2
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1


def test_two_imports_racing_publish_one_store(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    for _ in range(3):
        _seal()
    start = threading.Barrier(2)
    outcomes = []

    def run():
        start.wait()
        try:
            ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
            outcomes.append("imported")
        except (ledger_migrate.ImportRefused, ledger_store.LedgerLayoutError) as exc:
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("imported") == 1
    assert read_ledger(tmp_path / "capsule-ledger") == read_ledger(tmp_path / "ledger.jsonl")


def test_a_seal_that_waited_on_the_append_lock_follows_the_import(tmp_path):
    from capsule_emit import ledger as _ledger

    (tmp_path / "ledger.jsonl").touch()
    _seal()
    results = []
    with _ledger._append_lock:  # another seal in this process is writing
        waiting = threading.Thread(target=lambda: results.append(_seal()))
        waiting.start()
        waiting.join(0.5)
        assert waiting.is_alive()  # resolved to ./ledger.jsonl, blocked on the lock
        ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
    waiting.join(10)
    assert not waiting.is_alive()
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1
    store = read_ledger(tmp_path / "capsule-ledger")
    assert [c["capsule_id"] for c in store][-1] == results[0].capsule["capsule_id"]


# -- the warning, and the callers that name their own variable -----------------------


def test_the_warning_points_at_the_caller(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    with pytest.warns(LedgerDeprecationWarning) as caught:
        _seal()
    assert caught[0].filename == __file__


def test_a_warning_raised_as_an_error_is_issued_again(tmp_path):
    (tmp_path / "ledger.jsonl").touch()
    with warnings.catch_warnings():
        warnings.simplefilter("error", LedgerDeprecationWarning)
        with pytest.raises(LedgerDeprecationWarning):
            _seal()
    with pytest.warns(LedgerDeprecationWarning):
        _seal()


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.13.0", (0, 13, 0)), ("0.15.0rc1", (0, 15, 0)), ("0.15.0.dev3", (0, 15, 0)), ("0.14.2+local", (0, 14, 2)), ("dev", None)],
)
def test_installed_version_reads_pre_releases(monkeypatch, version, expected):
    import capsule_emit

    monkeypatch.setattr(capsule_emit, "__version__", version)
    assert ledger_layout._installed_version() == expected


def test_a_release_candidate_of_the_stop_release_refuses(tmp_path, monkeypatch):
    import capsule_emit

    (tmp_path / "ledger.jsonl").touch()
    monkeypatch.setattr(capsule_emit, "__version__", "0.15.0rc1")
    with pytest.raises(FlatLedgerWriteRefused):
        _seal()


def test_litellm_names_capsule_emit_ledger_in_its_warning(tmp_path, monkeypatch):
    pytest.importorskip("litellm")
    from capsule_emit.adapters.litellm_listener import listener_from_env

    (tmp_path / "proxy.jsonl").touch()
    monkeypatch.setenv("CAPSULE_EMIT_OPERATOR", "acme")
    monkeypatch.setenv("CAPSULE_EMIT_DEVELOPER", "gw@v1")
    monkeypatch.setenv("CAPSULE_EMIT_LEDGER", str(tmp_path / "proxy.jsonl"))
    monkeypatch.setenv("CAPSULE_LEDGER", str(tmp_path / "ignored"))
    listener = listener_from_env(anchor=False)
    with pytest.warns(LedgerDeprecationWarning, match=r"then set CAPSULE_EMIT_LEDGER=.*proxy-jsonl-store\."):
        listener.core.emit_capsule("tool.call")
    assert len(read_ledger(tmp_path / "proxy.jsonl")) == 1
    assert not (tmp_path / "ignored").exists()


def test_the_mcp_read_tools_never_refuse_a_path(tmp_path):
    pytest.importorskip("mcp")
    from capsule_emit import server

    new = str(tmp_path / "missing.jsonl")
    assert server.capsule_ledger(ledger=new).startswith("empty ledger")
    assert server.capsule_verify("ab" * 32, ledger=new).startswith("not_found")


def test_legacy_ledger_defaults_to_the_ledger_seal_writes(tmp_path):
    from capsule_emit.legacy_ledger import LegacyLedger

    assert LegacyLedger()._path == ledger_store.store_root(tmp_path / "capsule-ledger")
    (tmp_path / "ledger.jsonl").touch()
    assert LegacyLedger()._path == __import__("pathlib").Path("ledger.jsonl")


# -- Codex round 1: every entry point, holds' default, concurrent warnings, fixtures ---------


def _a_capsule(tmp_path):
    return _seal(ledger=tmp_path / "scratch").capsule


def test_append_to_ledger_follows_the_window_for_records_not_bookkeeping(tmp_path, monkeypatch):
    from capsule_emit.ledger import CHECKPOINT_STAMP_KIND, append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    with pytest.warns(LedgerDeprecationWarning):
        append_to_ledger(_a_capsule(tmp_path), flat)
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    with pytest.raises(FlatLedgerWriteRefused):
        append_to_ledger(_seal(ledger=tmp_path / "scratch").capsule, flat)
    stamp = {"capsule_id": "cd" * 32, "kind": CHECKPOINT_STAMP_KIND}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        append_to_ledger(stamp, flat)
    assert len(read_ledger_entries(flat)) == 2


def test_legacy_ledger_put_entry_follows_the_window(tmp_path, monkeypatch):
    from capsule_emit.artifact import UnsupportedError
    from capsule_emit.legacy_ledger import LegacyLedger, checked_record

    capsule = _a_capsule(tmp_path)
    record = checked_record(capsule["capsule_id"], capsule, None)
    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    with pytest.raises(FlatLedgerWriteRefused):
        LegacyLedger(flat).put_entry(record)
    with pytest.raises(UnsupportedError):
        LegacyLedger(flat).put(record)
    assert read_ledger_entries(flat) == []


def test_holds_default_to_the_ledger_seal_writes(tmp_path):
    from capsule_emit.holds import Action, HoldEngine

    action = Action(verb="money.transfer", operator="acme", developer="d", amount_minor=10, currency="USD")
    HoldEngine(cap_minor={"money.transfer": 100}).evaluate_and_reserve(action)
    assert len(read_ledger_entries(tmp_path / "capsule-ledger")) == 1


def test_holds_default_falls_back_to_an_existing_flat_ledger(tmp_path):
    from capsule_emit.holds import Action, HoldEngine

    (tmp_path / "ledger.jsonl").touch()
    action = Action(verb="money.transfer", operator="acme", developer="d", amount_minor=10, currency="USD")
    with pytest.warns(LedgerDeprecationWarning):
        HoldEngine(ledger_path=None, cap_minor={"money.transfer": 100}).evaluate_and_reserve(action)
    assert len(read_ledger_entries(tmp_path / "ledger.jsonl")) == 1
    assert not (tmp_path / "capsule-ledger").exists()


def test_two_threads_warn_once(tmp_path, monkeypatch):
    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    both = threading.Barrier(2, timeout=10)
    real_stopped = ledger_layout.flat_writes_stopped

    def meet_then_check(*args, **kwargs):
        both.wait()  # both threads are at the stop check, just before the warning's lock
        return real_stopped(*args, **kwargs)

    monkeypatch.setattr(ledger_layout, "flat_writes_stopped", meet_then_check)
    issued = []
    real_warn = warnings.warn

    def count(message, category=None, stacklevel=1, **kwargs):
        if category is LedgerDeprecationWarning:
            issued.append(message)
        else:
            real_warn(message, category, stacklevel + 1, **kwargs)

    monkeypatch.setattr(ledger_layout.warnings, "warn", count)
    threads = [threading.Thread(target=ledger_layout.check_new_flat_record, args=(flat,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(issued) == 1


def test_the_tutorial_fixtures_regenerate_over_the_committed_ones(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[2] / "docs" / "tutorials" / "fixtures"
    copy = tmp_path / "fixtures"
    shutil.copytree(fixtures, copy)
    run = subprocess.run([sys.executable, str(copy / "generate.py")], cwd=copy, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    for case in ("refund-support-agent", "incident-investigation-agent"):
        assert read_ledger_entries(copy / case / "ledger.jsonl")


def test_no_example_deletes_the_default_ledger():
    """An example that clears its ledger before a run must own that ledger:
    ./capsule-ledger/ is now every application's default (Codex round 2)."""
    import ast
    from pathlib import Path

    examples = Path(__file__).resolve().parents[2] / "examples"
    offenders = []
    for script in examples.rglob("*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        default_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and any(isinstance(c, ast.Constant) and c.value in (ledger_layout.DEFAULT_STORE, "./" + ledger_layout.DEFAULT_STORE) for c in ast.walk(node.value))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("rmtree", "unlink", "remove"):
                args = [*node.args, getattr(node.func, "value", None)]
                for arg in args:
                    if (isinstance(arg, ast.Name) and arg.id in default_names) or (
                        isinstance(arg, ast.Constant) and arg.value == ledger_layout.DEFAULT_STORE
                    ):
                        offenders.append(f"{script.relative_to(examples)}:{node.lineno}")
    assert offenders == []


def test_a_default_append_that_waited_while_an_import_published_the_store_goes_to_the_store(tmp_path, monkeypatch):
    from capsule_emit import ledger as _ledger
    from capsule_emit.ledger import append_to_ledger

    (tmp_path / "ledger.jsonl").touch()
    _seal()
    line = _seal(ledger=tmp_path / "scratch").capsule
    done = []
    with _ledger._append_lock:  # another write in this process holds the lock
        waiting = threading.Thread(target=lambda: done.append(append_to_ledger(line)))
        waiting.start()
        waiting.join(0.5)
        assert waiting.is_alive()
        ledger_migrate.import_ledger(tmp_path / "ledger.jsonl", tmp_path / "capsule-ledger")
    waiting.join(10)
    assert done
    assert len(read_ledger(tmp_path / "ledger.jsonl")) == 1
    assert [c["capsule_id"] for c in read_ledger(tmp_path / "capsule-ledger")][-1] == line["capsule_id"]


def test_a_warnings_hook_that_writes_a_ledger_does_not_deadlock(tmp_path, monkeypatch):
    from capsule_emit.ledger import append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    line = _seal(ledger=tmp_path / "scratch").capsule
    recorded = []

    def record_the_warning(message, category, filename, lineno, file=None, line_=None):
        recorded.append(append_to_ledger(_seal(ledger=tmp_path / "warnings-log").capsule, tmp_path / "warnings-log"))

    monkeypatch.setattr(warnings, "showwarning", record_the_warning)
    finished = []
    worker = threading.Thread(target=lambda: finished.append(append_to_ledger(line, flat)))
    with warnings.catch_warnings():
        warnings.simplefilter("always", LedgerDeprecationWarning)
        warnings.showwarning = record_the_warning
        worker.start()
        worker.join(10)
    assert not worker.is_alive()
    assert finished and recorded


def test_holds_name_the_variable_that_chose_their_flat_ledger(tmp_path, monkeypatch):
    from capsule_emit.holds import Action, HoldEngine

    (tmp_path / "audit.jsonl").touch()
    monkeypatch.setenv("CAPSULE_LEDGER", str(tmp_path / "audit.jsonl"))
    action = Action(verb="money.transfer", operator="acme", developer="d", amount_minor=10, currency="USD")
    with pytest.warns(LedgerDeprecationWarning, match="then set CAPSULE_LEDGER="):
        HoldEngine(cap_minor={"money.transfer": 100}).evaluate_and_reserve(action)


# -- Codex round 3 and the stand-in's delta check ---------------------------------


def test_a_new_nested_ledger_directory_is_created_with_its_parent(tmp_path):
    result = _seal(ledger="traces/capsule-ledger")
    assert read_ledger(tmp_path / "traces" / "capsule-ledger") == [result.capsule]


def test_resending_a_stored_line_stays_idempotent_after_the_window(tmp_path, monkeypatch):
    from capsule_emit.ledger import append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    line = _seal(ledger=tmp_path / "scratch").capsule
    with pytest.warns(LedgerDeprecationWarning):
        seq = append_to_ledger(line, flat)
    monkeypatch.setattr(ledger_layout, "_installed_version", lambda: (0, 15, 0))
    assert append_to_ledger(line, flat) == seq
    with pytest.raises(FlatLedgerWriteRefused):
        append_to_ledger(_seal(ledger=tmp_path / "scratch").capsule, flat)


def test_a_warnings_hook_that_writes_the_same_flat_ledger_does_not_recurse(tmp_path, monkeypatch):
    from capsule_emit.ledger import append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    lines = iter([_seal(ledger=tmp_path / "scratch").capsule for _ in range(3)])
    hooked = []

    def record_the_warning(message, category, filename, lineno, file=None, line=None):
        hooked.append(append_to_ledger(next(lines), flat))

    with warnings.catch_warnings():
        warnings.simplefilter("always", LedgerDeprecationWarning)
        warnings.showwarning = record_the_warning
        append_to_ledger(next(lines), flat)
    assert len(hooked) == 1
    assert len(read_ledger(flat)) == 2


# -- Codex round 4 and the stand-in's third delta check ----------------------------


@pytest.mark.parametrize("how", ["log", "explicit_signer", "append_to_ledger", "init"])
def test_every_write_creates_a_new_nested_ledgers_parent(tmp_path, how):
    from capsule_emit import log
    from capsule_emit.ledger import append_to_ledger
    from capsule_emit.legacy_ledger import LegacyLedger
    from capsule_emit.signing import LocalKeypairSigner

    target = tmp_path / "traces" / "capsule-ledger"
    if how == "log":
        log(b"event", operator="acme", witness=False, ledger=target)
    elif how == "explicit_signer":
        signer = LocalKeypairSigner(tmp_path / "key.pem")
        _seal(ledger=target, signer=signer)
    elif how == "append_to_ledger":
        append_to_ledger(_a_capsule(tmp_path), target)
    else:
        LegacyLedger(target).init()
    assert (target / "manifest.json").is_file()


def test_a_warning_raised_as_an_error_writes_nothing(tmp_path):
    from capsule_emit.ledger import append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    line = _a_capsule(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", LedgerDeprecationWarning)
        with pytest.raises(LedgerDeprecationWarning):
            append_to_ledger(line, flat)
    assert read_ledger_entries(flat) == []


def test_a_default_append_names_the_default_in_its_warning(tmp_path):
    from capsule_emit.ledger import append_to_ledger

    (tmp_path / "ledger.jsonl").touch()
    with pytest.warns(LedgerDeprecationWarning, match=r"or leave ledger= unset to use the default"):
        append_to_ledger(_a_capsule(tmp_path))


def test_a_resend_during_the_window_is_idempotent_under_warnings_as_errors(tmp_path, monkeypatch):
    from capsule_emit.ledger import append_to_ledger

    flat = tmp_path / "ledger.jsonl"
    flat.touch()
    line = _a_capsule(tmp_path)
    with pytest.warns(LedgerDeprecationWarning):
        seq = append_to_ledger(line, flat)
    monkeypatch.setattr(ledger_layout, "_warned", set())  # a fresh process
    with warnings.catch_warnings():
        warnings.simplefilter("error", LedgerDeprecationWarning)
        assert append_to_ledger(line, flat) == seq
