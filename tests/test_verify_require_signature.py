# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule-emit#185: ``verify`` printed VALID/rc 0 for a record
whose producer envelope (``signature``/``key_id``) was stripped after
signing -- ``verify_store_signed`` files that as a non-gating
``severity="warning"`` finding (correct for an honestly-unclaimed
:func:`capsule_emit.surface.log` entry) and ``_cmd_verify`` printed only
``severity="error"`` findings, so the warning never reached stdout either.

Covers:
- a stripped-envelope ledger stays ``VALID``/rc 0 by default, but the CLI
  now prints a one-line summary of the unclaimed-signature warning(s);
- ``--require-signature`` promotes the same finding to fatal (``INVALID``,
  rc 1);
- a genuinely bad signature is still ``INVALID`` regardless of the flag.

The existing pin that an honestly-unclaimed ``log()`` entry stays ``VALID``
by default lives in
``test_entry_authorship_tristate.py::test_verify_store_signed_grades_authored_unclaimed_invalid``
-- not duplicated here.
"""
from __future__ import annotations

import json

from capsule_emit import cli, seal
from capsule_emit import ledger as ledger_mod


def _sealed_two_record_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    seal(
        None, action="first", operator="acme", anchor=False,
        ledger=ledger_path, witness_url="http://127.0.0.1:1",
    )
    seal(
        None, action="second", operator="acme", anchor=False,
        ledger=ledger_path, witness_url="http://127.0.0.1:1",
    )
    return ledger_path


def _rewrite_one_record(ledger_path, mutate) -> None:
    """Rewrite the first non-checkpoint-stamp ledger line via ``mutate`` --
    same file-level forger shape as
    ``test_verify_authenticates_nothing_regressions._rewrite_ledger_line``."""
    lines = ledger_path.read_text().splitlines()
    out = []
    mutated = False
    for line in lines:
        entry = json.loads(line)
        if not mutated and entry.get("kind") != ledger_mod.CHECKPOINT_STAMP_KIND:
            entry = mutate(entry)
            mutated = True
        out.append(json.dumps(entry))
    assert mutated, "no capsule record found to mutate"
    ledger_path.write_text("\n".join(out) + "\n")


def _strip_envelope(record: dict) -> dict:
    record = dict(record)
    record.pop("signature", None)
    record.pop("key_id", None)
    return record


def _corrupt_signature(record: dict) -> dict:
    record = dict(record)
    good = record["signature"]
    record["signature"] = "00" * (len(good) // 2)
    return record


def test_stripped_envelope_ledger_is_valid_by_default_with_unclaimed_summary(tmp_path, capsys):
    ledger_path = _sealed_two_record_ledger(tmp_path)
    _rewrite_one_record(ledger_path, _strip_envelope)

    rc = cli.main(["verify", "--store", str(ledger_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "2/2 VALID" in out
    assert "INVALID" not in out
    assert "1 record(s) carry no producer signature (producer_signature_unclaimed)" in out


def test_stripped_envelope_ledger_with_require_signature_is_invalid(tmp_path, capsys):
    ledger_path = _sealed_two_record_ledger(tmp_path)
    _rewrite_one_record(ledger_path, _strip_envelope)

    rc = cli.main(["verify", "--store", str(ledger_path), "--require-signature"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "INVALID" in out
    assert "1/2 VALID" in out
    # the finding is now fatal, not a warning -- the warning-only summary
    # line must not additionally appear.
    assert "carry no producer signature" not in out
    assert "carry no producer signature" not in out


def test_bad_signature_is_invalid_by_default_and_with_require_signature(tmp_path, capsys):
    for extra_args in ([], ["--require-signature"]):
        case_dir = tmp_path / f"case{len(extra_args)}"
        case_dir.mkdir()
        ledger_path = _sealed_two_record_ledger(case_dir)
        _rewrite_one_record(ledger_path, _corrupt_signature)

        rc = cli.main(["verify", "--store", str(ledger_path), *extra_args])
        out = capsys.readouterr().out

        assert rc == 1, extra_args
        assert "INVALID" in out, extra_args
        assert "1/2 VALID" in out, extra_args


def test_fully_signed_ledger_output_is_unchanged(tmp_path, capsys):
    """Both arg sets on a fully signed ledger: no summary line, rc 0, 2/2 VALID."""
    ledger_path = _sealed_two_record_ledger(tmp_path)
    for extra in ([], ["--require-signature"]):
        rc = cli.main(["verify", "--store", str(ledger_path), *extra])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "2/2 VALID" in out
        assert "carry no producer signature" not in out
