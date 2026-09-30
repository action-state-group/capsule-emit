# Changelog

All notable changes to the `capsule-emit` Rust crate are documented here. The
Python package in this repository keeps its own changelog at the repository
root. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this crate uses [Semantic Versioning](https://semver.org/) once it reaches 1.0.

## 0.0.3 — unreleased

### Fixed
- A witness that missed checkpoints in this log's chain is caught up instead
  of refusing every later one. Push-time cuts are never offered on their own,
  and a registration that failed during an outage is superseded by the next
  checkpoint, so the witness could hold an older checkpoint than the one the
  offered checkpoint chains from and answer 409. On that 409 the crate reads
  the witness's last-accepted checkpoint from the body, sends every later
  checkpoint in `checkpoints.jsonl` in order, then resends the offered one;
  nothing is re-signed. A witness already at or past the checkpoint is dropped
  from pending. A witness holding a checkpoint this log does not have stays
  pending, and the log says a node that lost its local state must start a new
  log id.

## 0.0.2

### Changed
- `structure` rejects a malformed `references[].retention` declaration (§5.5.5)
  in check 1, as the reference verifier does since agent-action-capsule#149.
  Within each entry, in the reference's order:
  - `retention` present but not a JSON object (null included): `field_not_object`,
    and nothing else in it is checked;
  - `declarant` absent or null: `missing_required_field`, as for
    `disposition.approver` (a null `declarant` gave `field_not_string` in 0.0.1);
  - `retained_until` or `not_retained_after` of the wrong type: `field_not_string`;
  - neither bound present: `retention_empty`.

  A record that 0.0.1 accepted can now be refused. (#240)
- Vendored conformance vectors: the Class 1 capsule set is pinned to
  agent-action-capsule commit `6470239`, which adds the six `neg-retention-*`
  cases. (#240)

## 0.0.1 — 2026-09-29

First release: the Rust sibling of the Python `capsule-emit` package.

### Added
- `capsule`: seal a format-4 capsule from the core members plus caller-supplied
  `compute_attestation` extension members (committed in order, never
  interpreted; a key that collides with a core member is refused);
  `seal_local_record`; the inline producer envelope. (#230)
- `ledger`: the durable local ledger, with a chain-link check, restart and
  torn-write recovery, and caller-defined indexes. (#230)
- `jcs`, `cose`, `keys`, `checkpoint`, `padding`, `anchor`, `verify`,
  `sequence`, `timestamp`, and the `verify_capsule` binary. (#230)
- `structure`: the Class 1 checks on a capsule's own bytes (§6, no store): every
  error the reference verifier raises without a store, in its order and with
  its codes, plus its one defensive warning. `verify_offline` gates on it.
  Bounded on untrusted input. (#236)
- `structure` refuses a value of the wrong JSON type in a string-typed field
  with `field_not_string` (check 1), for the same fields and in the same order
  as the reference verifier (agent-action-capsule#147, #148). (#239)
- Vendored, checksum-gated conformance vectors: the agent-action-capsule Class 1
  capsule set and the provenance-mode set. (#236, #239)

### Fixed
- A path quoted in a `structure` finding is clipped as it is built, so a long
  key costs only what is kept. (#237)
