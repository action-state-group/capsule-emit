# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for the 0.5.0 migration audit, item 6 ("Stub mode + env
refusal", frozen dev-surface v4 §1a.4):

- ``CAPSULE_WITNESS=stub`` runs the real checkpoint mechanics with zero
  network, and the grade never leaves self-attested.
- ``CAPSULE_ENV=production`` with stub set refuses to run -- synchronously,
  before anything is written -- never a silent downgrade.
- the scream: a distinct first-use notice, and ``status`` marking a
  stub-only checkpoint loudly.

The CONDITION on this item (Steven, 2026-08-24, un-held 2026-08-24): the
normative stub marker's name and value (``cll-stub`` / ``true``) are now
fixed by draft-mih-scitt-checkpointed-local-log-00 ("Stub
Countersignatures") -- see ``tests/checkpoint/test_checkpoint_emit.py`` for
the marker-alignment tests. The real COSE wire encoding (a protected-header
parameter, listed in ``crit``) is separate COSE-wire work not yet landed;
until then ``register_checkpoint_stub`` emits a JSON placeholder using that
same marker name/value, so the literal "a third-party verifier reading only
the I-D grades a stub-stamped record self-attested" acceptance test still
needs the real COSE encoding and stays deferred to that follow-on work.
"""
from __future__ import annotations

import time

import pytest

import capsule_emit.core as core
from capsule_emit import seal, witness
from capsule_emit.checkpoint import Grade
from capsule_emit.status import compute_status, render_status


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    core._disclosure_printed = False
    yield
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    core._disclosure_printed = False


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(0.01)
        ok = predicate()
    return ok


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("stub mode must never touch the network")


# ---------------------------------------------------------------------------
# witness_mode / witness_is_stub -- the three-way resolution
# ---------------------------------------------------------------------------


def test_witness_mode_off_on_stub(monkeypatch):
    monkeypatch.delenv(witness.WITNESS_ENV_VAR, raising=False)
    assert witness.witness_mode(None) == "on"

    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "off")
    assert witness.witness_mode(None) == "off"

    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    assert witness.witness_mode(None) == "stub"
    assert witness.witness_is_stub(None) is True

    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "STUB")  # case-insensitive
    assert witness.witness_mode(None) == "stub"


def test_witness_mode_explicit_kwarg_always_overrides_stub_env(monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    assert witness.witness_mode(True) == "on"
    assert witness.witness_mode(False) == "off"
    assert witness.witness_is_stub(True) is False


def test_witness_enabled_is_true_for_both_on_and_stub(monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    assert witness.witness_enabled(None) is True
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "off")
    assert witness.witness_enabled(None) is False


# ---------------------------------------------------------------------------
# CAPSULE_ENV=production + stub set: hard, synchronous refusal
# ---------------------------------------------------------------------------


def test_stub_in_production_refuses_at_seal_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "production")
    ledger = tmp_path / "ledger.jsonl"

    with pytest.raises(witness.StubWitnessInProductionError):
        seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)

    assert not ledger.exists(), (
        "the refusal must fire before the capsule is ever appended to the ledger"
    )


def test_stub_in_production_refuses_via_maybe_checkpoint_direct(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "production")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")

    with pytest.raises(witness.StubWitnessInProductionError):
        witness.maybe_checkpoint(str(ledger), enabled=None)


def test_capsule_env_production_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "PRODUCTION")
    ledger = tmp_path / "ledger.jsonl"

    with pytest.raises(witness.StubWitnessInProductionError):
        seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)


def test_stub_outside_production_runs_fine(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.delenv(witness.CAPSULE_ENV_VAR, raising=False)
    ledger = tmp_path / "ledger.jsonl"

    capsule = seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)
    assert ledger.exists()
    assert capsule.capsule_id


def test_capsule_env_development_with_stub_runs_fine(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "development")
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)
    assert ledger.exists()


def test_production_without_stub_is_never_refused(tmp_path, monkeypatch):
    # CAPSULE_ENV=production is not itself an error -- only paired with
    # stub. Real witnessing explicitly off here so nothing dials out.
    monkeypatch.delenv(witness.WITNESS_ENV_VAR, raising=False)
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "production")
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger, witness=False)
    assert ledger.exists()


def test_production_explicit_witness_true_is_never_refused_by_stub_check(tmp_path, monkeypatch):
    # An explicit witness=True kwarg resolves to "on", never "stub" -- even
    # with CAPSULE_WITNESS=stub set in the environment (explicit always
    # wins) -- so this must not raise StubWitnessInProductionError. It will
    # warn about the unreachable default endpoint, which is unrelated.
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CAPSULE_ENV_VAR, "production")
    monkeypatch.setenv(witness.CADENCE_ENV_VAR, "1")
    ledger = tmp_path / "ledger.jsonl"

    seal(
        "payload", action="mint", operator="acme", anchor=False, ledger=ledger,
        witness=True, witness_url="http://127.0.0.1:1",
    )
    assert ledger.exists()


# ---------------------------------------------------------------------------
# zero network, real code path, grade never leaves self-attested
# ---------------------------------------------------------------------------


def test_stub_mode_makes_no_network_call_and_grade_stays_self_attested(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CADENCE_ENV_VAR, "1")
    monkeypatch.setattr("urllib.request.urlopen", _refuse_network)
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)

    key = witness._resolve_key(str(ledger))
    assert _wait_for(lambda: witness._states.get(key) is not None and witness._states[key].prev is not None)
    cp = witness._states[key].prev

    assert cp.witnesses, "the stub should still have produced a stamp"
    assert all(w.is_stub for w in cp.witnesses)
    assert cp.grade() == Grade.SELF_ATTESTED, (
        "frozen surface §1a.4: the stub's grade never leaves self-attested"
    )


def test_stub_mode_ignores_a_configured_witness_url_for_labeling(tmp_path, monkeypatch):
    # Even if CAPSULE_WITNESS_URL points somewhere real-looking, stub mode
    # must never dial it, and must not borrow its label -- the point is that
    # nothing that reads a stub WitnessRecord could mistake it for evidence
    # that a real, named endpoint was actually reached.
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CADENCE_ENV_VAR, "1")
    monkeypatch.setattr("urllib.request.urlopen", _refuse_network)
    ledger = tmp_path / "ledger.jsonl"

    seal(
        "payload", action="mint", operator="acme", anchor=False, ledger=ledger,
        witness_url="https://looks-real.example",
    )

    key = witness._resolve_key(str(ledger))
    assert _wait_for(lambda: witness._states.get(key) is not None and witness._states[key].prev is not None)
    cp = witness._states[key].prev
    assert all(w.ts_url != "https://looks-real.example" for w in cp.witnesses)


# ---------------------------------------------------------------------------
# scream surfaces: first-use notice, status
# ---------------------------------------------------------------------------


def test_stub_first_use_notice_is_distinct_and_prints_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)
    err = capsys.readouterr().err
    assert err.count("STUB WITNESS is armed") == 1
    assert "ZERO network" in err
    assert "self-attested" in err
    assert "CAPSULE_ENV=production" in err
    # never claims to actually send anything, unlike the real-witness notice:
    assert "will be sent to" not in err

    seal("payload-2", action="mint", operator="acme", anchor=False, ledger=ledger)
    err_after = capsys.readouterr().err
    assert "STUB WITNESS is armed" not in err_after, "must print at most once per process"


def test_stub_mode_never_prints_the_real_network_disclosure(tmp_path, monkeypatch, capsys):
    # core._print_first_run_disclosure_once's combined anchor+witness notice
    # must not fire on stub-only activity -- it would falsely claim a
    # network attempt.
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)
    err = capsys.readouterr().err
    assert "before this process's first network attempt" not in err


def test_status_marks_a_stub_only_checkpoint_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    monkeypatch.setenv(witness.CADENCE_ENV_VAR, "1")
    monkeypatch.setattr("urllib.request.urlopen", _refuse_network)
    ledger = tmp_path / "ledger.jsonl"

    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)
    key = witness._resolve_key(str(ledger))
    assert _wait_for(lambda: witness._states.get(key) is not None and witness._states[key].prev is not None)

    result = compute_status(str(ledger), offline=True)
    cp = result["latest_checkpoint"]
    assert cp is not None
    assert cp["grade"] == "self-attested"
    assert cp["stub_witness"] is True
    assert all(w["is_stub"] for w in cp["witnesses"])
    assert result["witnessing_mode_now"] == "stub"

    import io

    out = io.StringIO()
    render_status(result, out=out)
    rendered = out.getvalue()
    assert "STUB WITNESS" in rendered
    assert "self-attested" in rendered


def test_status_witnessing_mode_now_reports_stub(tmp_path, monkeypatch):
    monkeypatch.setenv(witness.WITNESS_ENV_VAR, "stub")
    ledger = tmp_path / "ledger.jsonl"
    seal("payload", action="mint", operator="acme", anchor=False, ledger=ledger)

    result = compute_status(str(ledger), offline=True)
    assert result["witnessing_mode_now"] == "stub"
    assert result["witnessing_enabled_now"] is True
