# Legacy ledgers as a storage backend

The flat ledgers that capsule-emit has always written keep working, and they
now sit behind the same two interfaces as every other backend:

- the CLL entry log, specified in checkpointed-local-log's
  `docs/backend-contract.md` (`cll.log.EntryStore` in Python,
  `cll::log::EntryStore` in Rust);
- the artifact store, specified in [artifact-store.md](artifact-store.md).

| Language | Ledger | Backend |
|---|---|---|
| Python | `ledger.jsonl` | `capsule_emit.legacy_ledger.LegacyLedger` |
| Rust | `capsules.jsonl` + `signed-statements/` | `capsule_emit::legacy_ledger::LegacyLedger` (crate `capsule-emit`) |

Legacy ledgers keep their own formats and are exempt from cross-language
storage interop. Existing ledgers open unchanged: the same lines, the same
sequence positions and the same checkpoint roots. `seal()` needs no
configuration and writes the same line it always has (Python). The one behavior change
is that sealing a `capsule_id` the ledger already holds no longer writes a
second line.

## The CLL entry log

Every line is a leaf. In Python: capsules, unsigned `log()` entries,
checkpoint stamps, disclosure records, witness backfills and padding. In Rust:
capsules and padding (Rust keeps its checkpoints in `checkpoints.jsonl`). For
each line:

| Entry field | Value |
|---|---|
| `seq` | The line's 1-based position among the lines that parse. Blank lines and unparseable lines are skipped, as the checkpoint index always has. |
| `value` | The line's `capsule_id`, 64 hex digits, as 32 bytes. Anything else is `corrupt`. |
| `appendedAt` | Derived, because the ledger stores no append time (below). |

`appendedAt` is the line's top-level `timestamp` (capsules, `log()` entries,
disclosure records), else its `checkpoint.timestamp` (Python checkpoint
stamps), else the previous entry's `appendedAt`, and the Unix epoch for a first
line with none. Times are read as RFC 3339 instants, where a missing offset
means UTC, and truncated to whole milliseconds. A time field that is
unparseable or outside the portable range counts as absent rather than
`corrupt`, so a historical ledger never becomes unreadable over a time it was
never required to carry.

### The contract exception: repeated digests

Python's ledger writer never deduplicated, so a historical file can hold the
same `capsule_id` on several lines, and each is a leaf. (Rust's chain linkage
already rules out a repeated capsule line, and padding carries a fresh nonce;
its backend applies the same rules.) The CLL contract is
idempotent by digest. The legacy backend keeps history unchanged:

- `scanEntries` returns every line, repeats included, so positions and roots do
  not move;
- `getEntry` returns the first occurrence;
- new writes are idempotent, so no new repeat is ever written.

This is the only deviation from the CLL contract.

Python's index is built incrementally: each operation reads only the bytes appended
since the last one. A file that shrinks or is replaced (another inode) is read
again from the start. Rewriting earlier bytes of the same file in place is not
detected, because the ledger is append-only.

### Writes

A ledger line is a whole record, never a bare digest, so the legacy backend
lacks the CLL `digest-append` capability. A capsule is written by artifact
`put`, which appends its line and therefore its log entry. CLL `append` returns
the existing entry as `idempotent` for a digest the ledger holds and fails with
`unsupported` otherwise.

The other leaves keep their writers, under the same writer lock:

- Python: checkpoint stamps, disclosure records and witness backfills through
  `capsule_emit.ledger.append_to_ledger`;
- Rust: padding through `put`, `LegacyLedger::append_padding` and
  `pad_to_bucket`, which call `Ledger::append_padding`.

A new stamp or padding line is a leaf like any other, so the next checkpoint
covers it. Rust keeps its indexed lookups (`Ledger` reads a record at its byte
offset) and its incremental checkpointing (`CheckpointState` folds only lines
past `consumed_bytes`).

### Checkpoint state (Python)

`LegacyLedger.load_cll()` reads the CLL checkpoint state (`cll.log.CllState`)
the ledger already holds. It writes nothing. The checkpoint is the newest
checkpoint-stamp line whose `checkpoint_cose` (the hex COSE_Sign1 statement
the witness thread stores beside the JSON checkpoint) verifies offline and
covers no more leaves than the ledger has. A stamp whose statement does not
verify is passed over for an older one. The MMR nodes are folded from the
ledger's own entries up to the checkpoint's size and must rebuild its signed
root; if they do not, the ledger is `corrupt`. A ledger with no such stamp
(none yet, or stamps written without a COSE statement) has no checkpoint:
`checkpoint` is `None`.

`commit_cll` is `unsupported`: checkpoint stamps keep their writer,
`capsule_emit.witness`.

With `load_cll`, `scan_entries` and the artifact `get` below, the ledger is
both inputs of agent-action-capsule's Evidence Bundle producer:

```python
from agent_action_capsule.bundle import build_evidence_bundle, proof_source_from_cll

from capsule_emit.legacy_ledger import LegacyLedger

ledger = LegacyLedger("ledger.jsonl")
bundle = build_evidence_bundle(proof_source_from_cll(ledger, ledger), root=capsule_id, interval_from=1)
```

`capsule-emit bundle ledger.jsonl` does the same from the command line. A
range that reaches a checkpoint-stamp, disclosure or backfill line is refused,
because those leaves are not records; start the interval after the last one.

## The artifact store

A capsule line carries its Producer Envelope inline: `signature` (hex
COSE_Sign1 over the `capsule_id` digest) and `key_id` (the signer's raw Ed25519
public key, hex). `get` returns:

- `capsule`: the line without `signature` and `key_id`, as the compact JSON the
  ledger writes;
- `producer_envelope`: the envelope bytes, or empty when the line has none
  (`log()` entries, and capsules sealed before producer envelopes).

Checkpoint stamps, disclosure records and witness backfills are log entries
only. `get` reports them `not_found`. A padding line is a record, in both
languages (see the contract's [padding records](artifact-store.md#padding-records)):
`get` checks and returns it like a capsule line, and `put` of a padding record
appends it as a padding line (in Rust, outside the chain). A padding line
without its producer envelope, or not in a padding record's shape, is
`invalid`.

Rust adds one attachment. Each line has a `signed-statements/<capsule_id>.cose`,
a COSE_Sign1 whose payload is the whole capsule line. `get` returns it as the
artifact `signed-statement`, `present` with no binding, so nothing about it is
authenticated by the Capsule. `put` must supply it (`invalid` otherwise),
because the ledger checks it on every reload, and `put` checks it the same way
before writing. A Rust capsule must also extend the ledger's chain: `put` of a
capsule whose `chain.parent_capsule_id` is not the current head is `invalid`.

### Verification

Legacy capsules are checked with capsule-emit's own rules. In Python, which
keeps both canonicalization profiles, `capsule_id` is recomputed per
`canonicalization_id`:

- `jcs`: SHA-256 of the JCS form without `capsule_id`, `signature` and
  `key_id`;
- `jcs-n`, also assumed when `canonicalization_id` is absent: `chain` is
  excluded too, and null members, empty arrays and empty objects are removed
  first.

A `jcs` capsule must also pass Class 1 verification, as in every other
artifact store; historical `jcs-n` capsules predate it and are checked by their
content address alone. The Go verifier accepts only `jcs`, so `jcs-n` capsules
are verified with capsule-emit's own verifier. Rust ledgers hold only `jcs`
capsules (`Ledger::open` recomputes every line under `jcs`), so Rust signed
records go through the artifact crate's own verification and pre-envelope
capsules through Class 1 and their content address.

When a line carries `signature` or `key_id`, both must be present, non-empty
strings, the envelope must verify over that `capsule_id`, and `key_id` must be
its signer. With trusted keys configured, the signer must also be one of them
(`untrusted_signer`). With none, any signer whose envelope verifies is
accepted, as `seal()` accepts whichever key it is configured with (Python
only; Rust requires trusted keys, as every artifact store does). Lines are
read as strict JSON, so a duplicated member is `invalid`.

`put` accepts only capsule bytes in the ledger's own line form, because the
file cannot hold other bytes for the same JSON value: compact `json.dumps`
output with ASCII escapes in Python, `serde_json` compact output in Rust.

In Python, `seal()` and `log()` write through the same append step as `put` but skip its
envelope check, because a custom `Signer` may return a signature that is not a
COSE envelope, which the ledger has always accepted. `get` then reports that
line `invalid`.

## Capability limits

Each limit is reported as a capability and refused with `unsupported`, after
the call's arguments are validated: `put` checks the record as it would for
storage (identity, capsule, envelope, signer) before it refuses an original.

| Capability | Legacy backend |
|---|---|
| CLL `digest-append` | No: write the capsule with `put` |
| CLL checkpoint-state writes | No: `commit_cll` is refused (Python; `load_cll` reads the stamps) |
| Originals | No: `put` with any declared artifact is refused (Rust: except the required `signed-statement`) |
| Purge | No: `purge` validates the ID, then refuses. Python has no originals to purge; Rust's statement is required by the ledger's reload check |
| Transactions | No `TxStore` extension |
| Writers | One. Python: a `flock` on `<ledger>.lock` excludes a second writer process (`LedgerLockedError`). Rust: the handle that opened the ledger; open no second one |

`capsule_emit.legacy_ledger.CAPABILITIES` and
`capsule_emit::legacy_ledger::CAPABILITIES` list them.

### Disclosing originals

Because the legacy ledger keeps no originals, `disclose()` refuses a
`LegacyLedger` as its `store`. An application still on a legacy `ledger.jsonl`
has two options:

- keep the ledger and put each capsule's originals in a separate,
  originals-capable artifact store, then pass that store to `disclose()`. The
  ledger line already holds the Capsule and its Producer Envelope, so the
  store record is `LegacyLedger.get(capsule_id)` with the originals added as
  artifacts;
- keep the originals elsewhere and pass them to `disclose()` through `reveal`.

```python
import json

from capsule_emit.artifact import AGENT_OUTPUT_DIGEST, PAYLOAD_DIGEST, PRESENT, Artifact
from capsule_emit.artifact.jsonl import JsonlArtifactStore
from capsule_emit.disclose import disclose
from capsule_emit.legacy_ledger import LegacyLedger

store = JsonlArtifactStore("originals.jsonl", "originals", [producer_public_key])
store.init()
legacy = LegacyLedger("ledger.jsonl")
try:
    record = legacy.get(capsule_id)
finally:
    legacy.close()
record.artifacts = [
    Artifact("payload", PRESENT, PAYLOAD_DIGEST, json.dumps(payload).encode()),
    Artifact("agent_output", PRESENT, AGENT_OUTPUT_DIGEST, json.dumps(output).encode()),
]
store.put(record)
d = disclose("ledger.jsonl", capsule_id, audience="auditor", store=store)
```

Each backend runs the subset of the shared conformance suites these limits
allow: the CLL vectors without `digest-append`, and the artifact vectors whose
cases need neither originals nor purge (the `bare_*` cases and
`malformed_ids`). The Rust harness adds the required statement to each record
before `put` and removes it after `get` (`conformance::Limits`). Legacy-specific
tests cover repeated capsule IDs, `jcs-n` and absent-`canonicalization_id`
capsules, non-capsule lines, new stamp and padding lines under the next
checkpoint, every CLI command (Python's commands, Rust's offline verifier), and
root invariance for a ledger with stamp, disclosure and backfill lines.
