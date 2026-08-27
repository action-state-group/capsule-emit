# capsule-emit slot-composition conformance vector

[v4-surface-complete-050] (2026-08-27) — the byte-level proof of O8's
acceptance criterion (`_work/dev-surface-v4-operational-2026-08-24.md`):
**"the carry-form and slot-form produce byte-identical records."** This is
the cross-language conformance target for `capsule-producer-go` (Ethan's
repo — coordinate with him, do not push there; see
`_work/ethan-v4-alignment-instructions-2026-08-24.md` §7).

- `valid/carry_form.json` — the full capsule from a standalone
  `received(mandate_jws, type="machine-mandate")` call.
- `valid/slot_form_composition.json` — the full composition capsule from
  `seal(who(...), can(<the exact carry_form Capsule object>), did(...))`.
- `valid/expected.json` — the assertion this vector proves:
  `slot_form_composition.composed_members[slot="can"].digest` equals
  `carry_form.capsule_id`, byte for byte — `can()` referenced the already-
  produced capsule, it did not re-mint one.
- `SHA256SUMS` — checksum manifest over the corpus.

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
every test run: the two capsules verify independently, and the byte-identity
assertion holds.
