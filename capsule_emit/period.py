# SPDX-License-Identifier: Apache-2.0
"""``--period week|month`` sugar over ``--since``/``--until`` (design §10.2:
"the ledger scan already takes since/until (ISO-8601) ... Add --period
week|month as sugar over since/until").

**Moved here from ``capsule_engine.cli.period``** ([emit-ledger-io-home],
2026-09-06). It lived in capsule-engine as an engine-local duplicate because
``capsule-ledger`` -- the shared ``ledger_io`` scan-query plumbing these
flags actually decorate -- was archived (read-only) on 2026-09-02 while that
task was in flight; now that ``ledger_io`` has a live home in
:mod:`capsule_emit.ledger_io`, this belongs beside it instead of duplicated
per verb. Each ledger-backed verb that wants ``--period`` adds this
module's argument and calls :func:`apply_period` before building its
``ScanQuery``.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

__all__ = ["PERIOD_CHOICES", "add_period_arg", "period_bounds", "apply_period"]

PERIOD_CHOICES = ("week", "month")

# The exact wire format ledger timestamps use (e.g.
# tests/fixtures/sample_ledger.jsonl): microsecond-precision UTC with a
# literal "Z" suffix. ``since``/``until`` are compared as a plain string
# against this by the underlying store -- a bound in a different but
# equal-instant format (e.g. Python's default "+00:00" offset suffix) would
# silently mis-order at the boundary, so the generated bounds MUST match
# this format exactly, not merely be valid ISO-8601.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def add_period_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--period",
        choices=PERIOD_CHOICES,
        default=None,
        help="sugar: fill in --since/--until with the current calendar week or month (UTC) wherever "
        "you did not set them explicitly",
    )


def period_bounds(period: str, *, anchor: datetime | None = None) -> tuple[str, str]:
    """Calendar bounds for ``period``, anchored to the current UTC instant by
    default. A CLI-convenience default, not part of the fold engine's own
    determinism rules (spec §3 rule 1) -- the engine only ever sees the
    concrete ISO-8601 bounds this resolves to; ``anchor`` exists so callers
    (and tests) can pin the "current instant" instead of depending on the
    wall clock at call time.

    ``week`` is the Monday-through-Sunday ISO calendar week; ``month`` is the
    calendar month -- both inclusive of their last microsecond, matching
    ``since``/``until``'s own "inclusive ISO-8601 bounds" contract.
    """
    now = anchor if anchor is not None else datetime.now(timezone.utc)
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7) - timedelta(microseconds=1)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            next_month_start = start.replace(year=start.year + 1, month=1)
        else:
            next_month_start = start.replace(month=start.month + 1)
        end = next_month_start - timedelta(microseconds=1)
    else:
        raise ValueError(f"unknown --period {period!r}; choices are {PERIOD_CHOICES}")
    return start.strftime(_TIMESTAMP_FORMAT), end.strftime(_TIMESTAMP_FORMAT)


def apply_period(args: argparse.Namespace) -> None:
    """Mutate ``args.since``/``args.until`` in place, filling in whichever
    one the caller did not already set explicitly -- sugar, not an
    exclusive mode; an explicit ``--since``/``--until`` always wins. A no-op
    when ``args.period`` is unset. Mutating ``args`` (rather than returning
    a new ``ScanQuery``) means every downstream reader of
    ``args.since``/``args.until`` -- ``build_scan_query``, ``echo_parts``,
    and a verb's own recorded query metadata -- sees the resolved bounds
    with no further plumbing."""
    period = getattr(args, "period", None)
    if period is None:
        return
    since, until = period_bounds(period)
    if getattr(args, "since", None) is None:
        args.since = since
    if getattr(args, "until", None) is None:
        args.until = until
