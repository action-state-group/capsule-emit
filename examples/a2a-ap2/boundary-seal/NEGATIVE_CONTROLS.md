# Negative controls for the boundary seal

A negative control is a value whose check must always DENY. It is only worth
something while it keeps denying, and only if a check notices when it stops.

## The all-zeros negative no longer denies

`negative_variant.json` uses the all-zeros `capsule_id`
(`0000…0000`, 64 zeros) as a fabricated id that "was never registered".
That held when the variant was written. But the all-zeros id is a well-formed
`capsule_id`, and the anchor registers any well-formed id it is sent. On
2026-10-05 at 01:02:23Z the anchor logged a registration of it (leaf index
2730). Since then `GET /v1/inclusion/0000…0000` returns 200 with an inclusion
proof, like any other registered entry, so this variant no longer denies.

The log is append-only and that entry stays. `negative_variant.json` and
`boundary_seal_rules.md` are left exactly as recorded (they are covered by
`SHA256SUMS`), and are superseded as follows.

## The negative to use: `negative_control.json`

Its `capsule_id` is `"z" * 64`: 64 characters like an id, but not hex. The
anchor's write routes refuse any `capsule_id` that is not 64 hex characters,
so this value can never be registered, and `GET /v1/inclusion/<it>` is refused
with 400. capsule-anchor's tests pin both, so a change that would let it be
registered fails them.

## Erratum: `boundary_seal_rules.md` §4

§4 lists "the anchor returns 404 for `GET /v1/inclusion/<capsule_id>` or
`POST /v1/digest`" as a DENY condition. `POST /v1/digest` never answers 404 for a
well-formed id: it **registers** the id it is given and returns its receipt.
Check a negative with `GET /v1/inclusion/<capsule_id>` only; it is a pure read
and never registers anything.

## The liveness check

`check_controls.py` checks every control with GETs only, and exits 1 naming
each one that changed:

| Control | Expected |
| --- | --- |
| positive (`553b0a23…f046`) | 200, entry hash `41ff4118…babbb`, leaf 167 |
| negative (`"z" * 64`) | 400 |
| a fresh random 32-byte id, drawn per run | 404 |

The fresh id covers the 404 path itself: a random 256-bit id has never been
registered, so a 200 there would mean the read route is broken, not that the
id was used. The workflow `.github/workflows/a2a-controls.yml` runs the check
daily and on demand.
