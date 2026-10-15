# Changelog

All notable changes to the `capsule-emit` Rust crate are documented here. The
Python package in this repository keeps its own changelog at the repository
root. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this crate uses [Semantic Versioning](https://semver.org/) once it reaches 1.0.

## Unreleased

- **No longer depends on `evidencebook`** (#321): `cargo tree -p capsule-emit`
  holds neither `evidencebook` nor `evidence-request`, and one
  `checkpointed-local-log` (0.3.1). The padding module is this crate's own:
  `build_padding_record`, `check_padding_shape`, `is_padding`,
  `fresh_store_nonce` and the constants keep their names and output (the
  parity fixture's padded ids are reproduced through the builder), and
  `build_padding_record` now returns this crate's `jcs::JcsError`. New:
  `padding_record_with_nonce`, for checking against conformance vectors.
  `tests/padding_vectors.rs` checks the builder and the shape check against
  the agent-action-capsule Evidence Layer padding vectors (vendored in
  `tests/vectors/aac-evidence-layer-padding/`).
  `check_padding_shape` now also requires `record_type` `padding` and
  `epistemic_type` `producer_claim`, as the Evidence Layer draft does and as
  every known writer (this crate, evidencebook, capsule-cli) writes them.
- The checkpoint path sits on `checkpointed-local-log` directly, as the
  Python producer does, instead of on `evidencebook`'s `CllSubstrate` (#321).
  `checkpoints.jsonl`, `mmr_nodes.dat`, checkpoint roots, signatures, COSE
  wire forms and inclusion proofs are unchanged byte for byte
  (`tests/evidencebook_parity.rs`, now with and without padding). The
  `checkpoint` module's public types keep their names, fields, methods and
  serde shapes (`CheckpointRecord`, `CheckpointWitness`, `InclusionProof`,
  `CheckpointSigner`, `OpenReport`, `verify_inclusion`), but are now this
  crate's own types rather than `evidencebook::substrate`'s, so code that
  passed them to `evidencebook` directly needs converting. **Breaking for a
  custom `CheckpointSigner`:** `sign` now returns
  `Result<[u8; 64], CheckpointSignError>` (exported from `checkpoint`)
  instead of `evidencebook::substrate::SignError`; an implementation changes
  that one type name. Signing with an `ed25519_dalek::SigningKey` is
  unchanged. `SubstrateError` (what `CheckpointRecord::leaf_count` returns)
  is exported too.
- Requires `checkpointed-local-log` 0.3.1.
- `Ledger::lines` lists every line (padding included) in file order as
  `LedgerLine`s, with `read_line` and `read_statement` to read one back, and
  the reload check `check_statement_matches` is public. The
  `capsule-emit-artifact` crate's legacy backend builds on them.

- Canonicalization is the `agent-action-capsule` crate's (0.1), the Rust
  sibling of the Go, Python and TypeScript references, instead of
  `evidencebook::canonical` (#321). Every existing vector, fixture and ledger
  `capsule_id` is unchanged. Differences, all on inputs no vector uses:
  - `-0` is the integer 0, written `0`, as the references do. It was refused
    as a float when this crate was built alone (and accepted in builds with
    `arbitrary_precision`, see below), so behavior no longer depends on the
    build.
  - An integer too large for 64 bits is reported as an unsafe integer, not
    a float, by `jcs` and by the structure check.
  - **Breaking:** `JcsError::UnsafeInteger` no longer carries the integer
    (it was `UnsafeInteger(i128)`, which cannot hold every unsafe integer).
    `JcsError::FloatInDigest` and `NotSerializable` are unchanged.
  - `jcs::normalize` (the withdrawn format-2 normalization, unused here) is
    removed.
  - `compute_capsule_id` stays the ungated digest primitive: it still names
    records without `format_version`, such as padding records.
- **serde_json `arbitrary_precision` is now on** wherever this crate is
  linked: `agent-action-capsule` requires it, and Cargo turns a feature on
  for the whole build. A number then keeps its source text. Under that
  feature, `serde_json`'s own decoder reads an object holding its private
  number token (`{"$serde_json::private::Number": ...}`) as a number or
  refuses it, so every record this crate reads (ledger recovery, `lookup`,
  `read_line`, statement payloads, offline verification, the checkpoint
  fold, `verify_capsule`) now goes through the new `jcs::decode_record`
  (AAC's strict decoder), which reads it as the object the references see.
  Callers that parse records themselves should use it too.
  `LedgerError::Decode` is new, and
  `CheckpointStateError::MalformedLine::source` is now a
  `jcs::DecodeError` (was `serde_json::Error`).

## 0.0.7

No code changes from 0.0.6. The first crate release through the release-PR
workflow (see RELEASING.md).

## 0.0.6

The first release after 0.0.4. 0.0.5 was never published: everything listed
under it below ships in 0.0.6.

### Changed — BREAKING
- `anchor`: no default service. `impl Default for AnchorClient`,
  `DEFAULT_ANCHOR_BASE` and `DEFAULT_WITNESS_URL` are removed: build a client
  with `AnchorClient::new(url)` and the URL you choose (a public witness is,
  for example, `https://witness.agentactioncapsule.org`).
- `anchor::dispatch_base_for` is removed: registration reaches each configured
  witness at exactly its configured URL. It used to send a witness configured
  as `https://witness.agentactioncapsule.org` to
  `https://anchor.agentactioncapsule.org`; the witness host serves the same
  routes itself.

- `anchor`: a redirect is never followed (`AnchorClient` is built with
  `redirects(0)`): a 3xx is returned as `AnchorError::Status`, so a request
  never reaches a host the caller did not name.
- `anchor`: every response body is read up to `MAX_RESPONSE_BYTES` (1 MiB);
  a larger one is `AnchorError::TooLarge` (a new variant). An error body is
  read under the same cap and kept to 4096 characters in the error.

### Added
- `AnchorClient::base_url()`: the URL a client sends to, as given.

## 0.0.5 (never published; included in 0.0.6)

### Fixed
- `checkpoint`: a registration with any configured witness other than the
  library's default URL was sent to the default anchor
  (`https://anchor.agentactioncapsule.org`, through the `AnchorClient` the
  caller passed in) and its receipt filed under the configured URL. The chosen
  witness was never contacted, a witness that was down never showed as
  pending, and a node sent its checkpoints to a witness it had not configured.
  Each configured witness URL is now reached through its own client
  (`dispatch_base_for(ts_url)`: the URL itself, or the default URL's alias),
  so a request for one witness never goes to another and none goes to a
  witness that is not configured. The `anchor` argument of `tick`,
  `checkpoint_covering`, `reconnect`, `checkpoint_on_shutdown` and
  `retry_pending_witnesses` is no longer used to register and is kept so
  callers do not change. The Python package was already correct
  (`cll.checkpoint.emit.register_checkpoint` dispatches each URL itself).
- `checkpoint`: when a witness that had missed checkpoints was caught up, the
  receipts it returned for those earlier checkpoints were dropped. They are now
  recorded in `witness-backfill.jsonl` like any receipt that arrives after its
  checkpoint was written, so with record push on (every checkpoint a push-time
  cut) each checkpoint the witness holds reads as witnessed.
- `checkpoint`: the clock leg cut one interval late. A backlog that arrives
  between ticks starts its age clock at the next tick, a little after that
  tick's scheduled instant, so the tick one interval later measured an age just
  short of `cadence_seconds` and waited a further interval (10 to 15 minutes
  instead of 5 by default). `tick` now allows for that: 5% of the cadence, at
  least one second. The shared due rule is unchanged.
- `checkpoint`: a witness receipt obtained after its checkpoint was written (a
  push-time cut, offered on a later tick, or a retried registration) was kept
  only in memory, so `checkpoints.jsonl` readers and restarts never saw it. It
  is now recorded beside the checkpoints in `witness-backfill.jsonl`, one line
  per receipt, and merged back on load. A receipt is merged only when it is
  bound to that checkpoint (`mmr_size`, `root`, and `entry_hash` =
  `sha256(bytes.fromhex(digest))`); its signature is not checked here, since
  that needs the witness's pinned key. Reading keeps complete lines only, skips
  each unreadable line on its own, and a write after a torn tail starts on a
  new line.

### Added
- `checkpoint::{WITNESS_BACKFILL_FILE, WitnessBackfill, read_witness_backfills,
  effective_witnesses}`: read those receipts, and a checkpoint's own witnesses
  merged with the ones bound to it (one per witness URL; the Python ledger's
  witness-backfill entries play the same role).

## 0.0.4

Producer side only: this crate builds, validates and seals settlement legs; it
does not derive settlement states. Verify legs with the Python reference
(`capsule_emit.settlement.verify_settlements`).

### Added
- `settlement`: two-party settlement records ("Two-Party Settlement Records for
  Agent Payments", draft-mih-agent-settlement-records-00). `build_leg` builds and
  validates a leg's top-level `settlement` member (`terms`, `payer_observed`
  with `amount` + `routing_fee`, `payee_observed` with `received` +
  `receive_fee`, `delivered` with `direction` + `content_digest`);
  `structure_failures` returns the draft's failure codes; `seal_leg` seals a leg
  as a local record; `counterparty_reference` cites the other party's leg.
  Deriving the settlement states is the Python reference verifier's job; a
  conformance test seals the legs of a real two-node Lightning run (1,000 msat
  sent, 995 credited, 5 fee, four invoices) and checks that the reference
  derives `agreed` for each. Another test pins the producer to the draft's
  conformance vectors: for every record of the 19 cases, `build_leg` rebuilds
  the vector's leg (JCS-equal) or refuses it with the expected codes, the
  `capsule_id` recomputes, and the producer envelope re-signs byte for byte.
- `capsule::seal_local_record_with_members`: a local record with extra
  top-level members, refusing any that would replace a member this crate
  writes. `seal_local_record` is unchanged.

## 0.0.3 — unreleased

### Fixed
- Committed times (a record's `timestamp`, a citing record's `received_at`, a
  checkpoint's `timestamp` and so its COSE `issued_at`) are minute-granular
  whole seconds with no fraction (`2026-09-27T17:08:00Z`), not
  `2026-09-27T17:08:00.000Z`. The TypeScript CLL verifier holds RFC 3339 times
  to one normalized form (no trailing-zero fraction) and refused every
  checkpoint this crate cut. `utc_now_minute` and `coarsen_to_minute` changed;
  `utc_now_iso8601` (local state only) keeps milliseconds. Records already
  sealed keep their times and stay valid. A test holds fresh checkpoints and
  records to that verifier's rule.

### Changed
- Requires `checkpointed-local-log` 0.2.1. 0.2.0 encoded a COSE checkpoint's
  claims map in insertion order, which verifiers that require deterministic
  CBOR (the TypeScript CLL verifier) refuse. 0.2.1 encodes it in RFC 8949
  §4.2.1 order, byte-identical to the Python reference. Checkpoints this crate
  signed before stay valid for the Python verifier, and need re-signing for the
  TypeScript one. New tests pin the cross-language checkpoint vectors
  (`tests/vectors/cll-checkpoint/`) and check that the checkpoints this crate
  cuts, chained ones included, sign their claims in that order.

### Fixed
- A witness that missed checkpoints in this log's chain is caught up instead
  of refusing every later one. Push-time cuts are never offered on their own,
  and a registration that failed during an outage is superseded by the next
  checkpoint, so the witness could hold an older checkpoint than the one the
  offered checkpoint chains from and answer 409. On that 409 the crate reads
  the witness's last-accepted checkpoint from the body, sends every later
  checkpoint in `checkpoints.jsonl` in order, then resends the offered one;
  nothing is re-signed. A witness that already holds one of this log's own
  checkpoints at or past the offered one is dropped from pending. A witness
  holding a checkpoint this log does not have (at any size) stays pending, and
  the log says a node that lost its local state must start a new log id.

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
