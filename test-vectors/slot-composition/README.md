# capsule-emit slot-composition conformance vector

The v4 slot-composition surface (2026-08-27) — the byte-level proof of the
acceptance criterion for the frozen v4 developer surface:
**"the carry-form and slot-form produce byte-identical records."** This is
the cross-language conformance target for `capsule-producer-go` (Ethan's
repo — coordinate with him, do not push there).

- `valid/carry_form.json` — the full capsule from a standalone
  `received(mandate_jws, type="machine-mandate")` call.
- `valid/slot_form_composition.json` — the full composition capsule from
  `seal(who(...), can(<the exact carry_form Capsule object>), did(...))`.
- `valid/expected.json` — the assertion this vector proves:
  `slot_form_composition.composed_members[slot="can"].digest` equals
  `carry_form.capsule_id`, byte for byte — `can()` referenced the already-
  produced capsule, it did not re-mint one.
- `valid-v05/` — the same three files for the `spec_version` -05 twin: the
  same calls with the same pinned key, uuids and timestamp, stamping
  `draft-mih-scitt-agent-action-capsule-05` as `seal()`/`received()` now do.
  Every Capsule ID differs; the can-slot byte-identity holds the same way.
  `valid/` shipped in v0.5.0 and is frozen byte-for-byte; the generator
  reproduces it exactly (it pins the stamped `spec_version` back to -04 for
  that case).
- `SHA256SUMS` — checksum manifest over the corpus (`valid/` lines first,
  unchanged; `valid-v05/` lines after).

Also demonstrates the new member-ref shape: each entry in
`composed_members` carries a `slot` key (`"who"|"can"|"did"|"audit"`)
alongside the CPB typed digest ref (`type`/`digest_alg`/`digest`) — new
relative to the v3 `compose()` shape, which had no `slot` field.

Regenerate deterministically (fixed test seed and pinned uuid4/timestamp —
public test material, never a production signing key — same convention as
`test-vectors/producer-envelope/`):

```bash
python test-vectors/slot-composition/scripts/generate_vectors.py
```

`tests/test_slot_composition_vectors.py` checks the checked-in corpus on
every test run, for both `valid/` and `valid-v05/`: the two capsules verify
independently, and the byte-identity assertion holds. Both cases also verify
under the Go and TypeScript references at `agent-action-capsule` main 439dc02
(the -05 wire).
