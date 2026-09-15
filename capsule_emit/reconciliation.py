# SPDX-License-Identifier: Apache-2.0
"""Reconcile two independently-sealed halves of the same exchange.

A **requester half** is a record this side sealed of a call it made (see
``capsule_emit.adapters.inspect_ai`` for the harness-boundary example). A
**counterparty half**, when one exists, is the other side's own sealed
record of the same exchange, correlated by whatever key both sides commit
to (a request nonce, a correlator, a sample/call id — see
``capsule_emit.bilateral`` for the cross-party correlator this project
already ships). This module does the fold: for each exchange, do the two
halves exist and agree?

Three states, not two — ``not present`` is a first-class outcome
------------------------------------------------------------------
- ``matched`` — both halves exist and their digests agree.
- ``requester_only`` — no counterparty half exists for this exchange. This
  is the honest state when the counterparty runs no producer at all (a
  plain API endpoint), or a producer that failed to seal, or hasn't been
  checked yet. It is never rendered ``failed`` and never left blank — an
  absent counterparty half is itself a fact worth stating, the same
  discipline ``capsule_emit.disclose`` applies to a withheld payload.
- ``contradicted`` — both halves exist and their digests disagree. This is
  the state a post-hoc transcript edit (METR's ``echo REAL`` → ``SPOOFTEST``
  finding) produces against a harness-sealed record of what actually
  executed — see the tests in ``tests/test_inspect_ai_adapter.py`` and
  ``tests/test_reconciliation.py`` for the vector, and the two mutants that
  must go red before the fix goes green.

What this establishes, and what it does not
---------------------------------------------
Reconciling ``matched`` establishes that two independently-signed records of
the same exchange **agree** — nothing here establishes that the exchange
"happened" in some stronger sense, and nothing here establishes that the two
signers are actually distinct parties. Whether a given counterparty key
belongs to a party genuinely independent of the requester is a **policy
input** a verifier supplies (who it is willing to trust as "the other
side"), not a property this module — or any signature scheme — can derive
from the records themselves. Nothing here rules out one operator holding
both keys and self-agreeing (Douceur, "The Sybil Attack", 2002). Say this
once, here; it is not repeated as a caveat on every call site.

What a fold counts, and how it is reported
---------------------------------------------
:func:`fold` counts exchanges by state. :func:`format_fold` renders each
count as ``"N of M"`` against the run's total — **never a percentage**: a
percentage discards the denominator a reader needs to judge whether 3
contradicted exchanges out of 4 is alarming or 3 out of 40,000 is noise.

Cost
----
Reconciliation is pure local computation over records both sides already
sealed — one linear pass to index the counterparty side by key, one linear
pass over the requester side. No network call, no additional signing, no
additional record. The cost of *having* a counterparty half to reconcile
against is whatever the counterparty's own producer already costs (see that
producer's own README) — this module adds nothing on top of it.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ReconciliationState",
    "ReconciledExchange",
    "reconcile",
    "fold",
    "format_fold",
    "CompletenessReport",
    "sample_completeness",
]


class ReconciliationState(str, Enum):
    """The three-state outcome of reconciling one exchange. See the module
    docstring for what each means and does not mean."""

    MATCHED = "matched"
    REQUESTER_ONLY = "requester_only"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class ReconciledExchange:
    """One exchange's reconciliation outcome.

    ``requester_digest`` / ``counterparty_digest`` are whichever digest
    field ``reconcile()`` was asked to compare (default
    ``agent_output_digest`` — the response half; pass ``digest_field=
    "agent_input_digest"`` to reconcile the request half instead).
    """

    key: str
    state: ReconciliationState
    requester_digest: str | None
    counterparty_digest: str | None
    detail: str


def _digest_of(record: dict[str, Any], digest_field: str) -> str | None:
    """Pull a digest out of a capsule's ``compute_attestation`` block.
    Absent when the record never committed that field — this returns
    ``None`` rather than raising, so a planned/errored record (which may
    have no ``agent_output_digest`` at all — see
    ``InspectAIListenerCore.seal_tool_call``) reconciles honestly instead of
    crashing the fold."""
    ca = (record.get("model_attestation") or {}).get("compute_attestation") or {}
    return ca.get(digest_field)


def reconcile(
    requester_records: Iterable[dict[str, Any]],
    counterparty_records: Iterable[dict[str, Any]],
    *,
    key_fn: Callable[[dict[str, Any]], str],
    digest_field: str = "agent_output_digest",
) -> list[ReconciledExchange]:
    """Reconcile each requester record against whatever counterparty record
    ``key_fn`` correlates it with.

    ``key_fn`` is the caller's choice of correlator — a bilateral
    ``cross_party.correlator``, an ``(sample_id, epoch, call_index)`` tuple
    rendered as a string, or any other key both sides can independently
    compute for the same exchange. This module has no opinion on what makes
    a good correlator; it only requires that both sides land on the same
    key for the same exchange.
    """
    counterparty_by_key = {key_fn(r): r for r in counterparty_records}
    results: list[ReconciledExchange] = []
    for req in requester_records:
        key = key_fn(req)
        req_digest = _digest_of(req, digest_field)
        cp = counterparty_by_key.get(key)
        if cp is None:
            results.append(
                ReconciledExchange(
                    key=key,
                    state=ReconciliationState.REQUESTER_ONLY,
                    requester_digest=req_digest,
                    counterparty_digest=None,
                    detail="no counterparty record found for this exchange — not present, not failed",
                )
            )
            continue
        cp_digest = _digest_of(cp, digest_field)
        if req_digest is not None and req_digest == cp_digest:
            state = ReconciliationState.MATCHED
            detail = "requester and counterparty digests agree"
        else:
            state = ReconciliationState.CONTRADICTED
            detail = "requester and counterparty digests disagree"
        results.append(
            ReconciledExchange(
                key=key,
                state=state,
                requester_digest=req_digest,
                counterparty_digest=cp_digest,
                detail=detail,
            )
        )
    return results


def fold(results: Sequence[ReconciledExchange]) -> dict[str, int]:
    """Count exchanges by state. Keys: ``matched``, ``requester_only``,
    ``contradicted``, ``total`` — ``total`` is the sum of the other three by
    construction (every result carries exactly one state)."""
    counts = {state.value: 0 for state in ReconciliationState}
    for r in results:
        counts[r.state.value] += 1
    counts["total"] = len(results)
    return counts


def format_fold(counts: dict[str, int]) -> dict[str, str]:
    """Render each count from :func:`fold` as ``"N of M"`` against the
    run's total. Never a percentage — see the module docstring."""
    total = counts["total"]
    return {
        state.value: f"{counts[state.value]} of {total}"
        for state in ReconciliationState
    }


@dataclass(frozen=True)
class CompletenessReport:
    """A found/not-found sample, the way METR checked completeness by hand:
    pull K records from one side, look for each on the other."""

    sampled: int
    found: int
    not_found: int
    not_found_keys: tuple[str, ...]

    def as_ratio(self) -> str:
        return f"{self.found} of {self.sampled}"


def sample_completeness(
    source_records: Sequence[dict[str, Any]],
    other_records: Iterable[dict[str, Any]],
    k: int,
    *,
    key_fn: Callable[[dict[str, Any]], str],
    rng: random.Random,
) -> CompletenessReport:
    """Sample ``k`` records from ``source_records`` and report how many of
    them are locatable (by ``key_fn``) among ``other_records``.

    ``rng`` is caller-supplied (``random.Random(seed)``) — this module never
    seeds its own randomness, so a completeness check is reproducible when
    the caller wants it to be and honestly non-deterministic when it
    doesn't pass a fixed seed.
    """
    k = min(k, len(source_records))
    sampled = rng.sample(list(source_records), k) if k else []
    other_keys = {key_fn(r) for r in other_records}
    not_found_keys = tuple(key_fn(r) for r in sampled if key_fn(r) not in other_keys)
    found = k - len(not_found_keys)
    return CompletenessReport(sampled=k, found=found, not_found=len(not_found_keys), not_found_keys=not_found_keys)
