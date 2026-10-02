# SPDX-License-Identifier: Apache-2.0
"""Time bounds compare instants, not strings. A record stamped in whole
seconds (``...23:59:59Z``, how this package commits times) and one stamped
with microseconds (``...23:59:59.999999Z``, older records) both fall inside a
bound that ends at the period's last microsecond; compared as strings, the
first sorts after it ('Z' > '.') and would drop out."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from capsule_emit.holds.aggregate import active_exposure_minor
from capsule_emit.period import period_bounds


def _hold(timestamp: str, amount: int) -> dict:
    return {
        "developer": "dev-1",
        "action_id": f"hold.reserve/{timestamp}",
        "timestamp": timestamp,
        "disposition": {"decision": "accept"},
        "asg_payload": {"amount_minor": amount},
    }


def test_as_of_compares_instants_at_the_last_second():
    as_of = "2026-08-31T23:59:59.999999Z"
    records = [
        _hold("2026-08-31T23:59:59Z", 1),  # whole seconds: inside, though it string-sorts after as_of
        _hold("2026-08-31T23:59:59.5Z", 10),  # a short fraction: inside
        _hold("2026-08-31T19:59:59-04:00", 100),  # another offset, same last second: inside
        _hold("2026-09-01T00:00:00Z", 1000),  # the next period's first instant: outside
    ]
    assert active_exposure_minor(records, "dev-1", as_of=as_of) == 111


def test_as_of_in_whole_seconds_still_excludes_a_later_fraction():
    records = [_hold("2026-08-31T23:59:59Z", 1), _hold("2026-08-31T23:59:59.000001Z", 10)]
    assert active_exposure_minor(records, "dev-1", as_of="2026-08-31T23:59:59Z") == 1


def test_a_record_with_an_unreadable_time_does_not_count_under_as_of():
    records = [_hold("not a time", 5), _hold("2026-08-01T00:00:00Z", 7)]
    assert active_exposure_minor(records, "dev-1", as_of="2026-08-31T23:59:59Z") == 7
    assert active_exposure_minor(records, "dev-1") == 12


def test_an_unreadable_as_of_is_refused():
    with pytest.raises(ValueError):
        active_exposure_minor([], "dev-1", as_of="yesterday")


@pytest.mark.parametrize("period", ["week", "month"])
def test_period_bounds_are_the_first_and_last_microsecond(period):
    anchor = datetime(2026, 8, 19, 15, 30, 45, 123456, tzinfo=timezone.utc)
    since, until = period_bounds(period, anchor=anchor)
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    assert start.time().isoformat() == "00:00:00" and end.time().isoformat() == "23:59:59.999999"
    # A whole-second record in the period's last second is inside the bound as
    # an instant (the comparison a store must make).
    last_second = end.replace(microsecond=0)
    assert start <= last_second <= end
