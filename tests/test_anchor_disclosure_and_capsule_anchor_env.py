# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [emit-anchor-disclosure-and-endpoint-consolidation],
updated for [O16-01-02] (per-seal ``anchor=True`` killed as a default /
single egress channel) and [O16-03] (the witness kill switch scopes ALL
egress, including this legacy channel):

- a first-run disclosure prints to stderr BEFORE the legacy anchor channel's
  network path is dispatched -- verified with a network-mocked first call
  that asserts the disclosure already landed at the moment the mock would
  have gone over the wire (the "red-then-green" shape: the assertion inside
  the mock fails against pre-fix code and passes post-fix).
- the notice covers only the path(s) actually active for that call.
- both paths disabled -> no notice, no network attempt.
- the legacy anchor channel is OFF BY DEFAULT as of 0.5.0: leaving both the
  ``anchor`` kwarg and ``CAPSULE_ANCHOR`` unset never dispatches it, and the
  old on-values (``"true"``/``"1"``/``"yes"``/unset) no longer enable it --
  only the exact value ``CAPSULE_ANCHOR=legacy-on`` does. An explicit
  ``anchor=`` kwarg always wins over the env var either direction.
- [O16-03] ``witness=False`` / ``CAPSULE_WITNESS=off`` kills the legacy
  anchor channel too, even when it is explicitly re-enabled -- so every test
  below that needs the legacy channel to actually fire now runs with witness
  ON (it never reaches its own cadence threshold in these single/few-call
  tests, so this adds no real network path).
- a missing optional anchor dependency (``scitt_cose`` not installed) is
  reported once, plainly, in the calling thread -- not as a cryptic
  per-capsule ``ModuleNotFoundError`` repr at interpreter shutdown -- when
  the legacy channel is explicitly engaged.
"""
from __future__ import annotations

import builtins

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
    core._stale_anchor_notice_printed = False
    witness._notice_printed = False
    witness._counts.clear()
    witness._armed_at.clear()
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
    core._stale_anchor_notice_printed = False
    witness._notice_printed = False
    witness._counts.clear()
    witness._armed_at.clear()
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
    passes once the disclosure is moved ahead of the dispatch. The legacy
    anchor channel is off by default as of 0.5.0, so this exercises the
    explicit ``anchor=True`` opt-in -- with witness explicitly ON, since
    O16-03 makes the legacy channel additionally subject to the witness kill
    switch (see ``test_witness_off_kills_legacy_anchor_...`` below)."""
    calls = []

    def fake_async_anchor(capsule_id, *, ts_url=None, **kw):
        assert core._disclosure_printed, (
            "async_anchor() was reached before the first-run disclosure printed"
        )
        calls.append(capsule_id)
        return _fake_future()

    monkeypatch.setattr(core, "async_anchor", fake_async_anchor)

    seal(
        {"x": 1}, action="test", operator="acme", anchor=True, witness=True,
        ledger=str(tmp_path / "ledger.jsonl"),
    )

    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "ANCHOR:" in err
    assert "capsule_id" not in err.split("ANCHOR:")[0]  # sanity: notice text, not a warning


def test_disclosure_prints_at_most_once_per_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    ledger = str(tmp_path / "ledger.jsonl")

    seal({"x": 1}, action="a", operator="acme", anchor=True, witness=True, ledger=ledger)
    capsys.readouterr()  # drain first notice
    seal({"x": 2}, action="b", operator="acme", anchor=True, witness=True, ledger=ledger)

    err = capsys.readouterr().err
    assert "ANCHOR:" not in err, "the disclosure must print at most once per process"


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


def test_no_disclosure_and_no_anchor_call_by_default(tmp_path, monkeypatch, capsys):
    """The core of [O16-01-02]: leaving ``anchor`` and ``CAPSULE_ANCHOR``
    both unset must never dispatch the legacy anchor channel, even with
    witness also off -- the per-seal anchor default is killed, full stop."""
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.setattr(
        core, "async_anchor", lambda *a, **kw: pytest.fail("anchor must not be attempted by default")
    )
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped"
    err = capsys.readouterr().err
    assert err == "", "no default network path is active -- nothing to disclose"


def test_disclosure_mentions_only_the_active_path(tmp_path, monkeypatch, capsys):
    """Anchor stays off by default, so witness-only (the 0.5.0 default) is
    the achievable "only one path active" case -- the reverse, anchor active
    with witness off, is now structurally impossible per O16-03 (the witness
    kill switch also gates the legacy anchor channel)."""
    seal(
        {"x": 1}, action="test", operator="acme",
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert "WITNESS:" in err
    assert "ANCHOR:" not in err


def test_witness_off_kills_legacy_anchor_even_when_explicitly_enabled(tmp_path, monkeypatch, capsys):
    """O16-03: the witness kill switch is the ONE switch that zeroes all
    egress -- including the legacy anchor channel, even when a caller has
    explicitly re-enabled it via ``anchor=True`` / ``CAPSULE_ANCHOR=legacy-on``.
    Before this fix, ``witness=False`` alone left anchor egress fully live."""
    monkeypatch.setenv("CAPSULE_ANCHOR", "legacy-on")
    monkeypatch.setattr(
        core, "async_anchor", lambda *a, **kw: pytest.fail("anchor must not be attempted -- witness kill switch is set")
    )
    r = seal(
        {"x": 1}, action="test", operator="acme", anchor=True, witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped", (
        "witness=False must kill the legacy anchor channel even with anchor=True + "
        "CAPSULE_ANCHOR=legacy-on both explicitly set"
    )
    err = capsys.readouterr().err
    assert err == "", "no active network path -- nothing to disclose"


# ---------------------------------------------------------------------------
# CAPSULE_ANCHOR env var: off by default as of 0.5.0, "legacy-on" is the only
# opt-in value, explicit-kwarg-always-wins either direction.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["off", "0", "false", "False", "FALSE", "no", "NO", "on", "1", "true", "yes", ""]
)
def test_capsule_anchor_env_non_legacy_values_all_skip_anchor(tmp_path, monkeypatch, value):
    """Every value except the exact ``"legacy-on"`` escape hatch leaves the
    legacy channel off -- including the OLD on-values, which is the
    deliberate breaking change: an existing ``CAPSULE_ANCHOR=true`` config
    must not silently keep double-egress alive across the 0.5.0 upgrade."""
    monkeypatch.setenv("CAPSULE_ANCHOR", value)
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped"


# ---------------------------------------------------------------------------
# [o16-fu-1-legacy-anchor-notice]: the stale pre-0.5.0 on-values are now a
# SILENT no-op (see the parametrized test above) -- these tests cover the
# one-time stderr notice that makes the downgrade audible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
def test_stale_capsule_anchor_on_value_prints_notice_once(tmp_path, monkeypatch, value, capsys):
    monkeypatch.setenv("CAPSULE_ANCHOR", value)
    ledger = str(tmp_path / "ledger.jsonl")

    seal({"x": 1}, action="a", operator="acme", witness=False, ledger=ledger)
    seal({"x": 2}, action="b", operator="acme", witness=False, ledger=ledger)

    err = capsys.readouterr().err
    assert err.count("now a no-op") == 1, (
        "the stale-value notice must print exactly once per process, not once per seal() call"
    )
    assert "legacy-on" in err, "the notice must say how to get the old behavior back"


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "legacy-on", ""])
def test_non_stale_capsule_anchor_values_never_print_notice(tmp_path, monkeypatch, value, capsys):
    monkeypatch.setenv("CAPSULE_ANCHOR", value)
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert "now a no-op" not in err


def test_unset_capsule_anchor_never_prints_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert "now a no-op" not in err


def test_explicit_anchor_kwarg_suppresses_stale_notice(tmp_path, monkeypatch, capsys):
    """An explicit ``anchor=`` kwarg always wins over ``CAPSULE_ANCHOR`` (see
    ``_anchor_enabled``), so the env var is never consulted for this call --
    no notice, since nothing was silently overridden."""
    monkeypatch.setenv("CAPSULE_ANCHOR", "true")
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    seal(
        {"x": 1}, action="test", operator="acme", anchor=True, witness=True,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    err = capsys.readouterr().err
    assert "now a no-op" not in err, "explicit anchor=True wins, so CAPSULE_ANCHOR is never consulted"


def test_capsule_anchor_legacy_on_value_enables_anchor(tmp_path, monkeypatch):
    # witness left at its default (on) -- O16-03 makes the legacy channel
    # additionally subject to the witness kill switch, so witness=False here
    # would mask what this test is checking (see the dedicated kill-switch
    # test above).
    monkeypatch.setenv("CAPSULE_ANCHOR", "legacy-on")
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    r = seal(
        {"x": 1}, action="test", operator="acme",
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status != "skipped", "CAPSULE_ANCHOR=legacy-on must re-enable the legacy anchor channel"


def test_explicit_anchor_true_overrides_capsule_anchor_off(tmp_path, monkeypatch):
    # witness left at its default (on) -- see the comment on
    # test_capsule_anchor_legacy_on_value_enables_anchor above.
    monkeypatch.setenv("CAPSULE_ANCHOR", "off")
    monkeypatch.setattr(core, "async_anchor", lambda *a, **kw: _fake_future())
    r = seal(
        {"x": 1}, action="test", operator="acme", anchor=True,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status != "skipped", "an explicit anchor=True kwarg must win over CAPSULE_ANCHOR=off"


def test_explicit_anchor_false_overrides_capsule_anchor_legacy_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_ANCHOR", "legacy-on")
    r = seal(
        {"x": 1}, action="test", operator="acme", anchor=False, witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped", (
        "an explicit anchor=False kwarg must win over CAPSULE_ANCHOR=legacy-on"
    )


def test_capsule_anchor_unset_defaults_to_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.setattr(
        core, "async_anchor", lambda *a, **kw: pytest.fail("anchor must not be attempted by default")
    )
    r = seal(
        {"x": 1}, action="test", operator="acme", witness=False,
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    assert r.anchor_status == "skipped", "unset CAPSULE_ANCHOR must leave the legacy channel off (0.5.0 default)"


# ---------------------------------------------------------------------------
# missing scitt_cose: fails seal() itself now, loudly
# ---------------------------------------------------------------------------
#
# **draft-04 reversal ([capsule-cose-sign1], 2026-08-24):** ``scitt_cose``
# stopped being an anchor-channel-only optional extra the moment every
# ``seal()``/``received()`` call started building a mandatory
# COSE_Sign1 producer envelope (``capsule_emit.signing.LocalKeypairSigner
# .sign_envelope``, reusing ``scitt_cose.cose_sign1``) -- it is now a hard
# runtime dependency of core sealing, pulled in transitively via
# ``agent-action-capsule[anchor,envelope]`` (see ``pyproject.toml``). A
# correctly installed capsule-emit therefore always has it; simulating its
# absence now means a broken install, and the correct behavior is to fail
# fast and clearly at the point of signing -- not to degrade gracefully
# (there is nothing to gracefully degrade: "every capsule is signed, always"
# has no off switch, per ``capsule_emit.core``'s module docstring).


@pytest.fixture
def _missing_scitt_cose(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "scitt_cose" or name.startswith("scitt_cose."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_missing_scitt_cose_fails_seal_fast_and_clearly(tmp_path, _missing_scitt_cose):
    ledger = str(tmp_path / "ledger.jsonl")
    with pytest.raises(ModuleNotFoundError, match="scitt_cose"):
        seal({"x": 1}, action="a", operator="acme", anchor=False, witness=False, ledger=ledger)
