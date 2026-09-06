# SPDX-License-Identifier: Apache-2.0
"""[emit-ledger-io-home] capsule_emit.period -- moved here from
capsule_engine.cli.period. Pure calendar-math + argparse-sugar unit tests;
end-to-end ``capsule bundle --period`` wiring stays in capsule-engine's own
suite (it owns the CLI that consumes this)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from capsule_emit.period import add_period_arg, apply_period, period_bounds

# A Wednesday, deliberately not a period boundary itself.
_ANCHOR = datetime(2026, 8, 19, 15, 30, 45, 123456, tzinfo=timezone.utc)


def test_period_bounds_week_is_the_monday_through_sunday_iso_week():
    since, until = period_bounds("week", anchor=_ANCHOR)
    assert since == "2026-08-17T00:00:00.000000Z"
    assert until == "2026-08-23T23:59:59.999999Z"


def test_period_bounds_month_is_the_calendar_month():
    since, until = period_bounds("month", anchor=_ANCHOR)
    assert since == "2026-08-01T00:00:00.000000Z"
    assert until == "2026-08-31T23:59:59.999999Z"


def test_period_bounds_month_rolls_over_the_year_boundary():
    since, until = period_bounds("month", anchor=datetime(2026, 12, 10, tzinfo=timezone.utc))
    assert since == "2026-12-01T00:00:00.000000Z"
    assert until == "2026-12-31T23:59:59.999999Z"


def test_period_bounds_rejects_an_unknown_period():
    with pytest.raises(ValueError):
        period_bounds("fortnight", anchor=_ANCHOR)


class _Args:
    def __init__(self, *, period=None, since=None, until=None):
        self.period = period
        self.since = since
        self.until = until


def test_apply_period_fills_in_since_and_until(monkeypatch):
    import capsule_emit.period as period_mod

    monkeypatch.setattr(period_mod, "period_bounds", lambda p, anchor=None: ("SINCE-STUB", "UNTIL-STUB"))
    args = _Args(period="month")
    apply_period(args)
    assert args.since == "SINCE-STUB"
    assert args.until == "UNTIL-STUB"


def test_apply_period_never_overrides_an_explicit_since(monkeypatch):
    import capsule_emit.period as period_mod

    monkeypatch.setattr(period_mod, "period_bounds", lambda p, anchor=None: ("SINCE-STUB", "UNTIL-STUB"))
    args = _Args(period="week", since="2026-01-01T00:00:00.000000Z")
    apply_period(args)
    assert args.since == "2026-01-01T00:00:00.000000Z"
    assert args.until == "UNTIL-STUB"


def test_apply_period_is_a_noop_without_period():
    args = _Args()
    apply_period(args)
    assert args.since is None
    assert args.until is None


def test_add_period_arg_enforces_choices():
    parser = argparse.ArgumentParser()
    add_period_arg(parser)
    assert parser.parse_args(["--period", "week"]).period == "week"
    with pytest.raises(SystemExit):
        parser.parse_args(["--period", "fortnight"])
