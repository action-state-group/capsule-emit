# Bilateral reconciliation — matched / requester only / contradicted

`capsule_emit.reconciliation` folds two independently-sealed halves of the
same exchange into one of three states per exchange, and counts a run's
exchanges by state. This page is the README the gate requires: what each
state means, what the fold does **not** establish, and the cost line.

## Background: why a second half

An agent's own transcript is an account the agent (or whatever executed on
its behalf) had the ability to write. METR's evaluation-integrity work found
≥20% of agent transcripts reasoning about tampering with the transcript
itself, and ~96 transcripts with a tool call's recorded result altered after
execution (`echo REAL` rewritten to read `SPOOFTEST`). A single account
cannot catch its own tampering. The fix in this project is architectural,
not a smarter reader of the same account: the **harness** — the process
running the eval, outside whatever sandbox executes the agent's tool
calls — seals its own record of every model call and every tool call it
executes on the agent's behalf, signed by a key the sandbox never has (see
`capsule_emit.adapters.inspect_ai`'s module docstring, "Where the signing
key lives"). Where a counterparty (a model provider, a tool backend) also
seals its own half, the two halves can be reconciled. `reconciliation.py` is
the fold that does that.

## The three states

- **`matched`** — both a requester half and a counterparty half exist for
  the exchange, and their digests agree. This is the strongest state the
  fold produces.
- **`requester_only`** — no counterparty half exists for this exchange.
  This is a first-class state, not an error and not a blank field. It is
  the honest outcome when the counterparty runs no producer at all (a plain
  API endpoint with no bilateral mechanism), or when a producer exists but
  its half hasn't been located yet. Rendering this as `failed` would claim
  something went wrong; rendering it blank would hide that the check was
  ever run. `requester_only` says exactly what is known: this side sealed
  its half, and no counterparty half was found.
- **`contradicted`** — both halves exist, and their digests disagree. This
  is the state a spoofed tool result (or any post-hoc edit to what a
  transcript shows) produces once reconciled against the harness's sealed
  record of what actually executed. See
  `tests/test_reconciliation.py::test_spoofed_tool_result_reconciles_as_contradicted`
  for the vector and the two mutants recorded alongside it.

## What a fold establishes, and what it does not

Reconciling `matched` establishes that two independently-signed records of
the same exchange **agree**. It does not establish that the exchange
"happened" in some stronger sense beyond that agreement, and — this is the
one point this project is careful to say only once, here, rather than
re-litigate at every call site — **it does not establish that the two
signers are distinct parties.** Whether a given counterparty key belongs to
someone genuinely independent of the requester is a **policy input** a
verifier supplies (who it is willing to trust as "the other side"), not a
property this module, or any signature scheme, can derive from the records
themselves. Nothing in a `matched` result rules out one operator holding
both keys and self-agreeing with itself (Douceur, "The Sybil Attack", 2002).
`capsule_emit.bilateral`'s cross-party rung derivation carries the same
caveat for the same reason.

Two more things a fold does not establish, restated from
`capsule_emit.adapters.inspect_ai`'s own "What this does NOT establish":

- **An agent that compromises the harness itself is out of scope.** What
  holds is that the sandbox executing the agent's tool calls cannot produce
  a record that verifies under the harness's key — that says nothing about
  a harness process that has itself been compromised, misconfigured to
  share its key with the sandbox, or run by an operator who chooses to
  self-agree (see the Sybil point above).
- **Events outside the sealed boundary don't exist here.** If neither side
  ever sealed a record of some call — a call the harness didn't route
  through its own capture, a provider that never ran the plugin — there is
  nothing for the fold to reconcile. Absence in the source stays absence in
  the fold; the fold reconciles what was sealed, not what happened.

## Reporting a fold: N of M, never a percentage

`fold()` counts exchanges by state; `format_fold()` renders each count as
`"N of M"` against the run's total. Never a percentage — "3 contradicted out
of 4" and "3 contradicted out of 40,000" both round to different stories a
percentage alone would flatten, and a reader needs the denominator to judge
either one.

## Completeness by counterparty trace

`sample_completeness()` is the same check METR ran by hand: pull `K` records
from one side, look for each on the other, report `found`/`not_found` as
`"N of K"`. It takes a caller-supplied `random.Random` — this module never
seeds its own randomness, so a completeness check is reproducible exactly
when the caller wants it to be.

## Cost

Reconciliation is pure local computation over records both sides already
sealed: one pass to index the counterparty side by key, one pass over the
requester side, no network call and no additional signing. The cost of
*having* a counterparty half to reconcile against is whatever that
producer's own emission costs — see, e.g., `capsule-emit`'s own witness
checkpoint cost line (`docs/checkpoint.md`) or a bilateral provider's own
README. This module adds nothing on top of what both sides already paid to
seal their own half.
