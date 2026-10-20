# SPDX-License-Identifier: Apache-2.0
"""Golden renders of ``capsule-emit status`` (capsule-emit#337).

The default text view speaks in receipts (sealed / witnessed / awaiting
witness) and carries no checkpoint vocabulary; ``--verbose`` adds the
checkpoint and stamp detail; ``--json`` is the unchanged ``compute_status``
dict. Each state below renders a hand-built ``compute_status`` result, so
the goldens pin every line exactly.
"""
from __future__ import annotations

import io
import json

import pytest

from capsule_emit import cli, seal, status

WITNESS = "https://witness.example"


def _result(**overrides):
    base = {
        "path": "ledger.jsonl",
        "offline": False,
        "capsule_count": 3,
        "checkpoint_count": 0,
        "records_awaiting_checkpoint": 3,
        "checkpoints_awaiting_stamp": 0,
        "witnessing_enabled_now": True,
        "witnessing_mode_now": "on",
        "configured_witness_urls": [WITNESS],
        "witness_backlog": {WITNESS: 0},
        "latest_checkpoint": None,
    }
    base.update(overrides)
    return base


def _checkpoint(*, grade="self-attested", grade_keys="none supplied", stub=False, witnesses=()):
    return {
        "mmr_size": 4,
        "leaf_count": 3,
        "grade": grade,
        "grade_keys": grade_keys,
        "stub_witness": stub,
        "timestamp": "2026-10-20T12:00:00Z",
        "key_id": "kid-1",
        "witnesses": list(witnesses),
    }


def _witness(**overrides):
    w = {
        "ts_url": WITNESS,
        "host": "witness.example",
        "is_stub": False,
        "confirmed": None,
        "kid_tail": None,
        "operator": "unknown",
    }
    w.update(overrides)
    return w


def _render(result, *, verbose=False):
    out = io.StringIO()
    status.render_status(result, out=out, verbose=verbose)
    return out.getvalue()


STATES = {
    "no_checkpoint_yet": (
        _result(),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
  awaiting witness              3
  witnessing (this process)     on

""",
    ),
    "self_attested_no_witness_reached": (
        _result(
            capsule_count=2,
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            checkpoints_awaiting_stamp=1,
            witness_backlog={WITNESS: 1},
            latest_checkpoint=_checkpoint(),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               2
  witnessed                     none confirmed
  awaiting witness              2
  witnessing (this process)     on

""",
    ),
    "receipts_held_but_no_keys_supplied": (
        _result(
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            latest_checkpoint=_checkpoint(
                witnesses=[_witness(confirmed=True, kid_tail="…9dc0afe1", operator="Example")]
            ),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
                                (witness receipts not checked: no witness keys supplied; pass --witness-directory)
  awaiting witness              3
  witnessing (this process)     on

  witnesses:
    witness: witness.example · key …9dc0afe1 · operated by: Example confirmed

""",
    ),
    "witnessed_with_newer_receipts_awaiting": (
        _result(
            capsule_count=5,
            checkpoint_count=1,
            records_awaiting_checkpoint=2,
            latest_checkpoint=_checkpoint(
                grade="witnessed",
                grade_keys="witness directory",
                witnesses=[_witness(confirmed=True)],
            ),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               5
  witnessed                     3
  awaiting witness              2
  witnessing (this process)     on

  witnesses:
    witness: witness.example · operated by: unknown     confirmed

""",
    ),
    "witness_receipt_not_confirmed": (
        _result(
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            latest_checkpoint=_checkpoint(
                grade_keys="trust anchor",
                witnesses=[_witness(confirmed=False, errors=["bad signature"])],
            ),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
  awaiting witness              3
  witnessing (this process)     on

  witnesses:
    witness: witness.example · operated by: unknown     NOT confirmed (bad signature)

""",
    ),
    "offline": (
        _result(
            offline=True,
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            latest_checkpoint=_checkpoint(
                grade="witnessed", grade_keys="trust anchor", witnesses=[_witness()]
            ),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     3
  awaiting witness              0
  witnessing (this process)     on

  witnesses:
    witness: witness.example · operated by: unknown     unconfirmed (--offline)

""",
    ),
    "witness_disabled": (
        _result(
            witnessing_enabled_now=False,
            witnessing_mode_now="off",
            configured_witness_urls=[],
            witness_backlog={},
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            latest_checkpoint=_checkpoint(grade_keys="trust anchor", witnesses=[_witness()]),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
  awaiting witness              3
  witnessing (this process)     off

  witnesses:
    witness: witness.example · operated by: unknown     unconfirmed (witness disabled)

""",
    ),
    "stub_witness": (
        _result(
            witnessing_mode_now="stub",
            checkpoint_count=1,
            records_awaiting_checkpoint=0,
            latest_checkpoint=_checkpoint(
                grade_keys="trust anchor",
                stub=True,
                witnesses=[_witness(is_stub=True, confirmed=False)],
            ),
        ),
        """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
                                ⚠ STUB WITNESS — proves nothing beyond self-attested
  awaiting witness              3
  witnessing (this process)     STUB (proves nothing beyond self-attested)

  witnesses:
    witness: witness.example · operated by: unknown     STUB — not a real Transparency Service

""",
    ),
}


@pytest.mark.parametrize("name", sorted(STATES))
def test_default_render_golden(name):
    result, golden = STATES[name]
    assert _render(result) == golden


@pytest.mark.parametrize("name", sorted(STATES))
def test_default_render_has_no_checkpoint_vocabulary(name):
    rendered = _render(STATES[name][0]).lower()
    for word in ("checkpoint", "stamp", "leaf", "mmr", "backlog"):
        assert word not in rendered


def test_verbose_render_golden_without_checkpoint():
    assert _render(_result(), verbose=True) == """
capsule-emit status: ledger.jsonl

  receipts sealed               3
  witnessed                     none confirmed
  awaiting witness              3
  witnessing (this process)     on

  checkpoint detail:
    checkpoints                   0
    latest checkpoint             none yet
    records awaiting checkpoint   3
    checkpoints awaiting stamp    0

"""


def test_verbose_render_golden_with_checkpoint_and_backlog():
    result = _result(
        capsule_count=5,
        checkpoint_count=2,
        records_awaiting_checkpoint=2,
        checkpoints_awaiting_stamp=1,
        witness_backlog={WITNESS: 1, "https://other.example": 0},
        latest_checkpoint=_checkpoint(
            grade="witnessed", grade_keys="witness directory", witnesses=[_witness(confirmed=True)]
        ),
    )
    assert _render(result, verbose=True) == """
capsule-emit status: ledger.jsonl

  receipts sealed               5
  witnessed                     3
  awaiting witness              2
  witnessing (this process)     on

  witnesses:
    witness: witness.example · operated by: unknown     confirmed

  checkpoint detail:
    checkpoints                   2
    latest checkpoint grade       witnessed
    latest checkpoint covers      3 leaf(ves) (mmr_size=4, at 2026-10-20T12:00:00Z)
    records awaiting checkpoint   2
    checkpoints awaiting stamp    1

    witness backlog (checkpoints awaiting THIS witness):
      https://witness.example                 1

"""


def _sealed_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ledger_path = tmp_path / "ledger.jsonl"
    seal(None, action="action-0", operator="acme", anchor=False, ledger=ledger_path, witness=False)
    return ledger_path


def test_cli_verbose_adds_checkpoint_detail(tmp_path, capsys, monkeypatch):
    ledger_path = _sealed_ledger(tmp_path, monkeypatch)

    assert cli.main(["status", str(ledger_path), "--offline"]) == 0
    default = capsys.readouterr().out
    assert cli.main(["status", str(ledger_path), "--offline", "--verbose"]) == 0
    verbose = capsys.readouterr().out

    assert "checkpoint" not in default
    assert verbose.startswith(default.rstrip("\n"))
    assert "records awaiting checkpoint   1" in verbose


def test_cli_json_is_the_compute_status_dict_unchanged(tmp_path, capsys, monkeypatch):
    ledger_path = _sealed_ledger(tmp_path, monkeypatch)
    expected = json.dumps(
        status.compute_status(str(ledger_path), offline=True), indent=2, default=str
    ) + "\n"

    assert cli.main(["status", str(ledger_path), "--offline", "--json"]) == 0
    assert capsys.readouterr().out == expected
    # --verbose only changes the text view.
    assert cli.main(["status", str(ledger_path), "--offline", "--json", "--verbose"]) == 0
    assert capsys.readouterr().out == expected
