# Provenance mode conformance vectors

Frozen conformance vectors for the `provenance_mode` block
(`../spec/draft-mih-scitt-agent-action-capsule-05.md`, "Provenance mode and
backfilled records" — a MODE on the ordinary Capsule, never a distinct
record type). Each case is a bare Capsule or `{"ledger": [...]}` plus the
expected Class 1 verifier result, version-pinned and hand-checkable.

## Why this lives outside `../test-vectors/`

`../test-vectors/` is the Class-1 conformance corpus for the base Agent
Action Capsule profile, and it is **cross-language-shared**: the Go
reference implementation's `vector_runner` (`../go/cmd/vector_runner/`)
reads `../test-vectors/vectors.json` directly and asserts every listed case
verifies identically under both implementations. The Go implementation has
never carried the `-02` `domain`/`provenance` addendum either (the Python
verifier's check 9 domain/provenance handling has always been Python-only,
with no vectors exercising it in `../test-vectors/`) — `provenance_mode`
joins that same Python-only surface rather than breaking Go conformance on
a feature it does not implement. This directory follows the same
frozen-vector discipline and file layout in a corpus of its own.

## These are DERIVED and FROZEN (not hand-authored)

Every `expected.json` is **derived** by running
`agent_action_capsule.verify()` / `verify_store()` over a hand-built
`input.json`, then **frozen**. A change to the result of any case is either
a spec/format revision (regenerate and review the diff) or a regression.
They regenerate via:

```bash
cd python && PYTHONPATH=. python3 scripts/generate_vectors.py provenance-mode
```

(the corpus name limits the run to this directory; the generator's other
corpora cannot currently be regenerated — see `main()` in the script).

The twelve cases released in v0.5.0 / go/v0.5.x carry `spec_version`
`draft-mih-scitt-agent-action-capsule-04` and are frozen byte-for-byte. Each
has a `<name>-v05` twin that differs only in `spec_version`
(`draft-mih-scitt-agent-action-capsule-05`) and therefore in `capsule_id`.

The expected values are **spec-anchored** — see
`draft-mih-scitt-agent-action-capsule-05.md`, "Provenance mode and
backfilled records" and "Class 1 verification" (check 9):

- `ok` — the Class 1 result, in the same shape as `../test-vectors/*/expected.json`
  (`ok`, `derived`, `capsule_id_recomputed`, `findings`).
- `derived.provenance_mode` / `derived.provenance_time_rung` — always reported
  when a `provenance_mode` block is present, independent of what the
  producer claimed: `provenance_time_rung` is rederived from evidence (a
  well-formed `references[]` entry citing `citation_purpose:
  "corroborates_source_time"`), never from the producer's own `time_rung`
  claim.
- Findings under `check: 9` — `provenance_mode_invalid`,
  `provenance_mode_missing_required_field`,
  `provenance_mode_source_ref_malformed`, `provenance_time_laundering_shape`,
  `provenance_time_rung_overclaim` (all gating — unlike the informational
  overclaim treatment check 7 gives `attestation_mode` / `ledger_mode` /
  `cross_party_rung`, this profile treats an unsupported `provenance_mode`
  time claim as a falsifiable dishonesty claim), and `duplicate_collapsed`
  (informational, store-level, `chain.relation: "duplicates"`).

## Layout

```
provenance-mode-vectors/
  README.md
  vectors.json              — manifest: every case with kind + one-line description
  SHA256SUMS                — pins every input.json / expected.json byte
  <case>/input.json         — a bare Capsule, or {"ledger": [...]}
  <case>/expected.json      — { ok, derived, capsule_id_recomputed, findings }, or { results: [...] } for a ledger case
```

## Cases

- **`pos-provenance-mode-backfilled`**: a well-formed backfilled record —
  `mode: "backfilled"` with all four REQUIRED companion fields
  (`source_ref`, `source_asserted_at`, `import_batch`, `imported_at`)
  present and well-formed, `time_rung` absent (implies `self_attested`).
  Verifies clean; `derived.provenance_mode`/`derived.provenance_time_rung`
  are always reported.
- **`neg-provenance-mode-time-rung-overclaim`**: `time_rung: "witnessed"`
  claimed with no `references[]` entry citing `citation_purpose:
  "corroborates_source_time"` — `provenance_time_rung_overclaim` (gating).
- **`neg-provenance-mode-backfilled-missing-fields`**: `mode: "backfilled"`
  with none of the four REQUIRED companion fields present — four
  `provenance_mode_missing_required_field` failures.
- **`neg-provenance-mode-time-laundering`**: `imported_at` equals
  `source_asserted_at` — the shape a laundering producer would construct to
  make a backfilled import look contemporaneous — `provenance_time_laundering_shape`
  (gating; equality is never treated as corroboration).
- **`pos-chain-duplicates-collapsed-once`** (store): a backfilled import of
  the same logical event a contemporaneous capsule already recorded,
  chained to it via `chain.relation: "duplicates"`. Both capsules verify
  `ok`; the store-level pass reports `duplicate_collapsed` (info) on the
  backfilled member.

## Running

`python/tests/test_provenance_mode_vectors.py` runs every case in this
directory through `verify()` / `verify_store()` and asserts each
`expected.json`. To check an independent implementation, run it over each
`input.json` and compare `ok`, `derived`, `capsule_id_recomputed`, and the
`(check, severity, code)` tuple of each finding.
