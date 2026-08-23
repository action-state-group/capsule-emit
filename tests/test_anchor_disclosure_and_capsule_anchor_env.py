# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [emit-anchor-disclosure-and-endpoint-consolidation]:

- a combined first-run disclosure (anchor + witness) prints to stderr BEFORE
  either default network path is dispatched -- verified with a network-mocked
  first call that asserts the disclosure already landed at the moment the
  mock would have gone over the wire (the "red-then-green" shape: the
  assertion inside the mock fails against pre-fix code and passes post-fix).
- the notice covers only the path(s) actually active for that call.
- both paths disabled -> no notice, no network attempt.
- ``CAPSULE_ANCHOR`` env var: off-values disable anchor when the ``anchor``
  kwarg is left at its default; an explicit ``anchor=`` kwarg always wins.
- a missing optional anchor dependency (``scitt_cose`` not installed) is
  reported once, plainly, in the calling thread -- not as a cryptic
  per-capsule ``ModuleNotFoundError`` repr at interpreter shutdown.
"""
from __future__ import annotations

import builtins
import warnings

import pytest

import capsule_emit.core as core
import capsule_emit.witness as witness
from capsule_emit import seal


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    core._disclosure_printed = False
    core._dep_notice_printed = False
    core._anchor_deps_checked = False
    core._anchor_deps_available = True
    witness._notice_printed = False
    witness._counts.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.delenv("AAC_ANCHOR_URL", raising=False)
    monkeypatch.delenv("CAPSULE_WITNESS", raising=False)
    yield
    core._disclosure_printed = False
    core._dep_notice_printed = False
    core._anchor_deps_checked = False
    core._anchor_deps_available = True
    witness._notice_printed = False
    witness._counts.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()


def _fake_future():
    from agent_action_capsule.anchor import AnchorFuture

    future = AnchorFuture()
    future._set(object())  # done, non-None, non-AnchorError sentinel
    return future


# ---------------------------------------------------------------------------
# disclosure prints before any network attempt (red-then-green)
# ---------------------------------------------------------------------------


def test_disclosure_prints_before_anchor_network_attempt(tmp_path, monkeypatch, capsys):
    """The historical bug: emit()'s first call anchored before any disclosure.
    This mock asserts the disclosure already printed at the exact moment the
    (mocked) network call would fire -- fails against the pre-fix code path,
    passes once the disclosure is moved ahead of the dispatch."""
    calls = []

    def fake_async_anchor(capsule_id, *, ts_url=None, **kw):
        assert core._disclosure_printed, (
            "async_anchor() was reached before the first-run disclosure printed"
        )
        calls.append(capsule_id)
        return _fake_future()

    monkeypatch.setattr(core, "async_anchor", fake_async_anchor)

    seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )

    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "ANCHOR:" in err
    assert "WITNESS:" not in err  # witness=False on this call
    assert "capsule_id" not in err.split("ANCHOR:")[0]  # sanity: notice text, not a warning


def test_disclosure_prints_at_most_once_per_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    ledger = str(tmp_path / "ledger.jsonl")

    seal({"x": 1}, action="a", operator="acme", witness=False, ledger=ledger)
    capsys.readouterr()  # drain first notice
    seal({"x": 2}, action="b", operator="acme", witness=False, ledger=ledger)

    err = capsys.readouterr().err
    assert "on by default" not in err, "the combined disclosure must print at most once per process"


def test_no_disclosure_and_no_network_when_both_paths_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        core, "async_anchor", lambda *a, **kw: pytest.fail("anchor must not be attempted")
    )
    seal(
        {"x": 1}, action="test", operator="acme", anchor=False, witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert err == "", "no default network path is active -- nothing to disclose"


def test_disclosure_mentions_only_the_active_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert "ANCHOR:" in err
    assert "WITNESS:" not in err


# ---------------------------------------------------------------------------
# CAPSULE_ANCHOR env var: off-values, explicit-kwarg-wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["off", "0", "false", "False", "FALSE", "no", "NO"])
def test_capsule_anchor_env_off_values_skip_anchor(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CAPSULE_ANCHOR", value)
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped"


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", ""])
def test_capsule_anchor_env_on_values_leave_anchor_enabled(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CAPSULE_ANCHOR", value)
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status != "skipped"


def test_explicit_anchor_true_overrides_capsule_anchor_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_ANCHOR", "off")
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    r = seal(
        {"x": 1}, action="test", operator="acme", anchor=True, witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status != "skipped", "an explicit anchor=True kwarg must win over CAPSULE_ANCHOR=off"


def test_explicit_anchor_false_overrides_capsule_anchor_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_ANCHOR", "true")
    r = seal(
        {"x": 1}, action="test", operator="acme", anchor=False, witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped", "an explicit anchor=False kwarg must win over CAPSULE_ANCHOR=true"


def test_capsule_anchor_unset_defaults_to_on(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status != "skipped", "unset CAPSULE_ANCHOR must leave the pre-existing on-by-default behavior"


# ---------------------------------------------------------------------------
# missing optional anchor dependency: plain, one-time, not cryptic-per-call
# ---------------------------------------------------------------------------


@pytest.fixture
def _missing_scitt_cose(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "scitt_cose" or name.startswith("scitt_cose."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_missing_dependency_prints_plain_notice_once(tmp_path, _missing_scitt_cose, capsys):
    ledger = str(tmp_path / "ledger.jsonl")
    for i in range(3):
        seal({"x": i}, action=f"a{i}", operator="acme", witness=False, ledger=ledger)

    err = capsys.readouterr().err
    assert err.count("the optional SCITT anchor dependency isn't installed") == 1, (
        "the plain dependency notice must print exactly once, not once per capsule"
    )
    assert "pip install" in err


def test_missing_dependency_does_not_warn_cryptically_at_exit(tmp_path, _missing_scitt_cose):
    """Historical bug: a bare ModuleNotFoundError repr surfaced only as a
    RuntimeWarning at interpreter shutdown, for whichever futures happened to
    still be pending. Once the plain notice has fired, the atexit sweep must
    not also emit the cryptic per-capsule warning for the same cause."""
    ledger = str(tmp_path / "ledger.jsonl")
    seal({"x": 1}, action="a", operator="acme", witness=False, ledger=ledger)
    # Give the background worker a moment to fail (fast: import error, no I/O).
    import time

    time.sleep(0.2)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core._join_pending_anchors_at_exit()

    cryptic = [w for w in caught if "ModuleNotFoundError" in str(w.message)]
    assert cryptic == [], f"expected no cryptic per-capsule warning, got: {cryptic}"


def test_missing_dependency_never_reports_anchored_true(tmp_path, _missing_scitt_cose):
    r = seal(
        {"x": 1}, action="a", operator="acme", witness=False, anchor_wait=2.0,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchored is False
    assert r.anchor_status == "failed"
