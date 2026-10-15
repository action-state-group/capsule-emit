# Legacy ledgers as a storage backend

The flat ledgers that capsule-emit has always written keep working, and they
now sit behind the same two interfaces as every other backend:

- the CLL entry log, specified in checkpointed-local-log's
  `docs/backend-contract.md` (`cll.log.EntryStore` in Python);
- the artifact store, specified in [artifact-store.md](artifact-store.md).

| Language | Ledger | Backend |
|---|---|---|
| Python | `ledger.jsonl` | `capsule_emit.legacy_ledger.LegacyLedger` |

Legacy ledgers keep their own formats and are exempt from cross-language
storage interop. Existing ledgers open unchanged: the same lines, the same
sequence positions and the same checkpoint roots. `seal()` needs no
configuration and writes the same line it always has. The one behavior change
is that sealing a `capsule_id` the ledger already holds no longer writes a
second line.

## The CLL entry log

Every line is a leaf: capsules, unsigned `log()` entries, checkpoint stamps,
disclosure records, witness backfills and padding. For each line:

| Entry field | Value |
|---|---|
| `seq` | The line's 1-based position among the lines that parse. Blank lines and unparseable lines are skipped, as the checkpoint index always has. |
| `value` | The line's `capsule_id`, 64 hex digits, as 32 bytes. Anything else is `corrupt`. |
| `appendedAt` | Derived, because the ledger stores no append time (below). |

`appendedAt` is the line's top-level `timestamp` (capsules, `log()` entries,
disclosure records), else its `checkpoint.timestamp` (checkpoint stamps), else
the previous entry's `appendedAt`, and the Unix epoch for a first line with
none. Times are read as RFC 3339 instants, where a missing offset means UTC, and
truncated to whole milliseconds.

### The contract exception: repeated digests

The ledger writer never deduplicated, so a historical file can hold the same
`capsule_id` on several lines, and each is a leaf. The CLL contract is
idempotent by digest. The legacy backend keeps history unchanged:

- `scanEntries` returns every line, repeats included, so positions and roots do
  not move;
- `getEntry` returns the first occurrence;
- new writes are idempotent, so no new repeat is ever written.

This is the only deviation from the CLL contract.

### Writes

A ledger line is a whole record, never a bare digest, so the legacy backend
lacks the CLL `digest-append` capability. A capsule is written by artifact
`put`, which appends its line and therefore its log entry. CLL `append` returns
the existing entry as `idempotent` for a digest the ledger holds and fails with
`unsupported` otherwise.

The other leaves keep their writers, under the same writer lock: checkpoint
stamps, disclosure records and witness backfills go through
`capsule_emit.ledger.append_to_ledger`. A new stamp line is a leaf like any
other, so the next checkpoint covers it.

## The artifact store

A capsule line carries its Producer Envelope inline: `signature` (hex
COSE_Sign1 over the `capsule_id` digest) and `key_id` (the signer's raw Ed25519
public key, hex). `get` returns:

- `capsule`: the line without `signature` and `key_id`, as the compact JSON the
  ledger writes;
- `producer_envelope`: the envelope bytes, or empty when the line has none
  (`log()` entries, and capsules sealed before producer envelopes).

Checkpoint stamps, disclosure records, witness backfills and padding are log
entries only. `get` reports them `not_found`.

### Verification

Legacy capsules are checked with capsule-emit's own rules, which keep both
canonicalization profiles. `capsule_id` is recomputed per
`canonicalization_id`:

- `jcs`: SHA-256 of the JCS form without `capsule_id`, `signature` and
  `key_id`;
- `jcs-n`, also assumed when `canonicalization_id` is absent: `chain` is
  excluded too, and null members, empty arrays and empty objects are removed
  first.

A present envelope must verify over that `capsule_id`, and `key_id` must be
its signer. With trusted keys configured, the signer must also be one of them
(`untrusted_signer`). With none, any signer whose envelope
verifies is accepted, as `seal()` accepts whichever key it is configured
with. Class 1 structure checks stay with `capsule-emit verify`. The Go
verifier accepts only `jcs`, so historical `jcs-n` capsules are verified with
capsule-emit's own verifier.

`put` accepts only capsule bytes in the ledger's own line form, because the
file cannot hold other bytes for the same JSON value: compact `json.dumps`
output with ASCII escapes.

`seal()` and `log()` write through the same append step as `put` but skip its
envelope check, because a custom `Signer` may return a signature that is not a
COSE envelope, which the ledger has always accepted. `get` then reports that
line `invalid`.

## Capability limits

Each limit is reported as a capability and refused with `unsupported`:

| Capability | Legacy backend |
|---|---|
| CLL `digest-append` | No: write the capsule with `put` |
| Originals | No: `put` with any declared artifact is refused |
| Purge | No: `purge` validates the ID, then refuses; there are no originals to purge |
| Transactions | No `TxStore` extension |
| Writers | One: a `flock` on `<ledger>.lock` excludes a second writer process (`LedgerLockedError`) |

`capsule_emit.legacy_ledger.CAPABILITIES` lists them.

The backend runs the subset of the shared conformance suites these limits
allow: the CLL vectors without `digest-append`, and the artifact vectors whose
cases need neither originals nor purge (the `bare_*` cases and
`malformed_ids`). Legacy-specific tests cover repeated capsule IDs, `jcs-n` and
absent-`canonicalization_id` capsules, non-capsule lines, new stamp
lines under the next checkpoint, every CLI command, and root invariance for a ledger with stamp,
disclosure and backfill lines.
