# `capsule_emit.checkpoint` — the CLL (Checkpointed Local Log) core

## Default wiring (since 0.5.0)

`capsule_emit.core.emit()` wires this in **by default** — no opt-in code
required. Every ledger you `emit()` into participates in a checkpoint/witness
stream automatically:

- **Cadence: 100 entries or 15 minutes, whichever comes first, both
  configurable.** Once a ledger accumulates
  `capsule_emit.witness.DEFAULT_CADENCE_ENTRIES` (100) entries since its
  last checkpoint — **or** `capsule_emit.witness.DEFAULT_CADENCE_SECONDS`
  (900) seconds have elapsed since the first unwitnessed entry after the
  last checkpoint — a signed peaks checkpoint over that ledger's MMR is
  built and registered with a Transparency Service — the same free
  public-good tier the legacy per-emit anchor channel also uses,
  `witness.agentactioncapsule.org` (semantically a witness, not the anchor —
  **[currently served via `anchor.agentactioncapsule.org`; the `witness.`
  CNAME is pending]**, see `capsule_emit.checkpoint.emit. DEFAULT_TS_URL` /
  `_PENDING_CNAME_TARGETS`). This is the **only default egress channel** as
  of 0.5.0 — the older per-emit anchor channel is now an explicit,
  non-default opt-in (see
  [`docs/why-anchoring.md`](why-anchoring.md#in-practice)), not something
  every default `emit()` call also dispatches.
- **An idle log is silent, never a heartbeat.** The age leg is checked
  lazily, only inside `witness.maybe_checkpoint` — which itself only ever
  runs right after a real `emit()` call appends a new entry. There is no
  background timer or polling thread, so a ledger with no new activity is
  never checkpointed on age alone: it is structurally impossible, not a
  runtime guard. Checkpoint-stamp entries (see below) reinforce this —
  they're written directly through `ledger.append_to_ledger`, never through
  `core.emit()`, so persisting a stamp never advances the entry counter
  *or* resets the age clock.
- **Multiple witnesses.** `witness_url=` (and `CAPSULE_WITNESS_URL`) accept a
  single endpoint or several — a list, or a comma-separated string — and the
  same checkpoint is registered with *every* endpoint named, independently;
  one endpoint failing never blocks the others. A single default endpoint is
  used unless you opt into more.
- **Digest-only** — the only bytes that ever cross the wire are the
  checkpoint's own SHA-256 digest (a hash of hashes; see `emit.py`'s
  `CheckpointRecord.digest()`). No capsule content, no ledger path, no
  action names, ever leave the process. Same posture as the anchor.
- **Async, fire-and-forget** — the checkpoint build (local, no network) and
  its TS registration (the only network call) run on a daemon thread; a
  cadence-crossing `emit()` call never blocks on it.
- **Lazy** — nothing above is imported or computed until a checkpoint is
  actually due. A caller who calls `emit()` once and exits, or whose ledger
  never crosses the cadence threshold, pays zero cost: no MMR built, no
  `capsule_emit.checkpoint` import, no network dependency touched. This is
  what keeps `import capsule_emit` (and a single below-cadence `emit()`
  call) exactly as cheap as before this default flipped on.

**First-use notice.** `capsule-emit` prints one line to stderr, once per
process, at the first `emit()`/`seal()` call where witnessing is enabled —
before the first byte ever leaves the process, not gated on a checkpoint
actually being due (the default cadence is 100 entries, so a short-lived
process might otherwise never trigger one and never see the notice). It
states what will be sent (a 32-byte digest, structurally incapable of
carrying capsule content), where (the resolved endpoint(s)), and how to turn
it off. It never prints a second time in the same process.

**Turning it off:**

```python
emit(..., witness=False)          # this call's ledger opts out
```

```bash
export CAPSULE_WITNESS=off        # opt out everywhere, no code change
```

An explicit `witness=` kwarg always overrides the env var. Repoint the
endpoint (or add more) with `emit(..., witness_url=...)` or
`CAPSULE_WITNESS_URL=…`; override the entry-count cadence with
`CAPSULE_WITNESS_CADENCE_ENTRIES=…` and the age-based cadence with
`CAPSULE_WITNESS_CADENCE_SECONDS=…`.

### Kill switch scope (O16-03)

**`witness=False` / `CAPSULE_WITNESS=off` is ONE switch that zeroes ALL
egress, not just the checkpoint stream.** It also gates:

- **`status`'s stamp-fetch** — the read-only GET that independently
  re-confirms a witness receipt (see [Checking status](#checking-status---capsule-emit-status)
  below). It skips this network call whenever the kill switch is set, even
  if you didn't also pass `--offline` — `status ./ledger.jsonl` on a process
  with `CAPSULE_WITNESS=off` never touches the network, full stop.
- **The legacy anchor channel** — even if it was explicitly re-enabled via
  `anchor=True` / `CAPSULE_ANCHOR=legacy-on` (see
  [`docs/why-anchoring.md`](why-anchoring.md#in-practice)), the witness kill
  switch overrides it. An `anchor=True, witness=False` call never dispatches
  the legacy channel.

This is what makes the **local-only posture** (turning witnessing off) an
honest, absolute "nothing leaves this process" guarantee rather than one
that a separately-configured legacy channel or a `status` call could quietly
poke a hole in. A no-network test in CI asserts zero egress across all three
paths simultaneously.

**What trust tier this reaches — be precise.** A single-TS default checkpoint
is **witnessed (single witness)**: it upgrades the stream from
*self-attested* to third-party-checkable — the witness vouches that the
records under this checkpoint **existed, in that order, and haven't been
rewritten since** (existence + order + non-deletion). It does **not** vouch
that the records' *content* is true, and it is **not** the *multi-witness,
equivocation-resistant* tier described in
[why anchoring makes it trustworthy](why-anchoring.md#be-precise-about-what-it-proves-and-doesnt)
— that tier specifically requires witnesses a verifier can cross-check: the
same checkpoint independently co-signed by, or registered to, more than one
independently-operated log. Register the default checkpoint with more than
one Transparency Service (`emit(..., witness_url=[url1, url2])` or a
comma-separated `CAPSULE_WITNESS_URL`) to climb from single-witness to
multi-witness; the zero-config default does not do this for you.

**Signing.** The default path signs checkpoints with the SAME persisted
Ed25519 identity (`capsule_emit.signing.LocalKeypairSigner`) that signs
capsule content — resolved with the identical precedence `seal()` uses
(`signer=`/`signing_key_path=`/`CAPSULE_SIGNING_KEY_PATH`/a key file next to
the ledger). A checkpoint signed in one process therefore verifies in a
later one, including by a stranger who holds only the checkpoint bytes (see
"Bundle" below, and `checkpoint.verify_checkpoint_signature_offline`) — this
replaced an earlier ephemeral, in-process-only HMAC key
(`witness._AutoSigner`) specifically because that could never be verified
again once the signing process exited. Use the manual API below with your
own `Signer` if you want a different identity.

## Test & dev — the stub witness

`CAPSULE_WITNESS=stub` runs the identical checkpoint mechanics — MMR sync,
checkpoint build, signature, stamp persistence — against a local, in-process
stub instead of a real Transparency Service: **zero network**, and the grade
**never leaves self-attested**, no matter how many stub stamps accumulate
(`CheckpointRecord.grade()` excludes stub-sourced `WitnessRecord`s from the
witnessed any-of). Use it for unit tests, CI, and eval-before-procurement —
it exercises the real code path, not a mock, with no endpoint to stand up and
no network flake to work around.

```bash
export CAPSULE_WITNESS=stub       # this process's checkpoints run through the stub
```

**`CAPSULE_ENV` × `CAPSULE_WITNESS` matrix:**

| `CAPSULE_ENV`           | `CAPSULE_WITNESS=stub`                          | anything else |
|--------------------------|--------------------------------------------------|----------------|
| `production`              | **refuses to run** — `StubWitnessInProductionError`, raised synchronously at the first `seal()`/`received()`, before anything is written | normal (real witness, or off) |
| unset / anything else     | stub mode runs; a scream prints once to stderr at the first stub-armed `seal()`, and `status`/`--json` mark the checkpoint `"stub_witness": true` | normal |

Three hard rules, enforced in code, not just documented:

1. **Explicit opt-in only, never a fallback.** An unreachable real witness
   endpoint is a warning and a retry concern (see the idle-silence /
   `status` lag numbers above) — it never silently downgrades to stub.
2. **`CAPSULE_ENV=production` + stub set is a startup error**, not a
   warning — see the matrix above.
3. **Stub stamps never reach rung 2.** `status` reports a stub-only latest
   checkpoint as `self-attested` with an explicit `⚠ STUB WITNESS` line, and
   each stub `WitnessRecord` in `witnesses` is labeled `is_stub: true`.

**The normative stub marker.** The CLL I-D's "Stub Countersignatures" section
(draft-mih-scitt-checkpointed-local-log-00) defines it: a stub
countersignature's COSE protected header MUST carry the parameter
`cll-stub` (label TBD1, pending IANA assignment) with value `true`, and
MUST list that label in `crit` ({{Section 3.1 of RFC9052}}) — a verifier
that recognizes `cll-stub` treats the countersignature as conferring no
witnessing; one that doesn't rejects it under `crit` processing. Either way
the result is unwitnessed, never witnessed — exactly what `grade()`'s stub
exclusion enforces here. `capsule_emit.checkpoint.STUB_MARKER` is
`"cll-stub"`, matching the spec's name and value. What's still pending is
the wire encoding, not the marker itself: capsule-emit's stub receipts are
still a JSON placeholder (`register_checkpoint_stub`'s `receipt_b64`), not a
real COSE_Sign1 countersignature — that lands with separate COSE-wire work.
The placeholder already uses `cll-stub: true` (plus a `crit`-shaped list
naming it), so a real Transparency Service's `verify_receipt_offline` never
mistakes it for a real receipt today, and only the encoding — not the
marker's name or value — changes once COSE-wire work ships.

## Direct / manual use

The primitives below remain independently usable, and stay opt-in in the
original sense — nothing in `capsule_emit`'s top-level import path touches
them; `import capsule_emit` alone never loads an MMR module, never pulls in
a checkpoint dependency. Reach for this directly when you want your own
cadence, your own persisted signing key, or a Transparency Service other than
the default:

```python
from capsule_emit.checkpoint import MmrLedger, CheckpointConfig, emit_checkpoint
```

## What it is

A Merkle Mountain Range (MMR) index over your own capsule ledger, plus a
signed, tamper-evident **peaks checkpoint** you can optionally register with a
Transparency Service (TS) for independent, third-party freshness evidence.

- **`core`** — the pure MMR algorithm: position math, domain-separated
  hashing, inclusion/consistency proofs. No I/O, MMRIVER-draft-compatible.
- **`store`** — an in-memory node backing (`MemoryNodeStore`).
- **`index`** — `MmrLedger`, a decorator over any object shaped like
  `LogSource` (`append`/`scan`/`fetch`/`find_gaps`/`verify`, matched
  structurally — never by importing a concrete log implementation). Wraps
  your own append-only capsule log with inclusion and range proofs.
- **`emit`** — builds, signs, and (optionally) registers a checkpoint:
  `{log_id, mmr_size, root, prev_size, prev_root, key_id, timestamp,
  signature}`, plus `witnesses` once one or more Transparency Services have
  stamped it. `key_id` doubles as a peer identifier when a deployment
  checkpoints several independent logs (e.g. one per mesh node).

## Checkpoint/stamp persistence — the stamp is a log entry, not just an in-memory field

Since 0.5.0, `capsule_emit.core.emit()`'s default wiring (`capsule_emit.witness`)
writes every checkpoint it builds — signature, `mmr_size`, and whatever
`witnesses` it collected — back into the *same ledger it covers*, as its own
JSONL line:

```json
{"kind": "checkpoint_stamp", "v": 1, "capsule_id": "<checkpoint.entry_digest()>",
 "checkpoint": {"v": 1, "kind": "mmr_checkpoint", "log_id": "...",
                "mmr_size": 100, "root": "...", "prev_size": 0,
                "prev_root": "", "key_id": "...", "timestamp": "...",
                "signature": "...", "witnesses": [...]}}
```

This is not a re-issue of any capsule and does not change any capsule's
`capsule_id` — it is a distinct entry that becomes an MMR leaf the *next*
checkpoint's `mmr.sync()` folds in. The leaf is addressed by
`CheckpointRecord.entry_digest()` — a hash over the *entire* persisted entry
(`to_dict()`: signing body, `signature`, and `witnesses` alike) — not
`CheckpointRecord.digest()` (the signing-body-only value registered with the
TS). That is what "checkpoint N's stamp is covered by checkpoint N+1" means
in practice: the history carries the evidence of its own witnessing, so
flipping or deleting a byte of a persisted stamp's `witnesses` changes its
leaf and breaks the covering checkpoint's root, rather than that evidence
living only in the `CheckpointRecord.witnesses` list of a process-local
object a restart discards. Stamp entries never wake the cadence/idle
timer — they aren't written through `core.emit()`, so they never touch
`witness.maybe_checkpoint`'s per-`emit()`-call counter *or* its age clock;
persisting a stamp neither advances the entry count nor resets (nor starts)
the 15-minute window (`tests/test_witness_stamp_persistence.py`,
`tests/test_witness_idle_silence_and_age_cadence.py`).

`kind`/`v` are the entry's format-version marker: `capsule_emit.ledger.read_ledger`
filters `checkpoint_stamp` entries out by default, so every capsule-only
consumer (the CLI, `ledger.view`/`view_chains`/`show`, `server`, `permalink`,
`approval`, `holds`) keeps seeing exactly the capsule stream it always has,
unaffected by this change. Use `capsule_emit.ledger.read_ledger_entries` to
read the raw file, stamps included — that's what the checkpoint layer's own
MMR indexing (`capsule_emit.witness._JsonlLogSource.scan`) does, so stamp
entries are indexed as leaves too.

## Checking status — `capsule-emit status`

```bash
capsule-emit status ./ledger.jsonl              # renders text, re-checks the latest witness receipt
capsule-emit status ./ledger.jsonl --offline    # local-only, no network call
capsule-emit status ./ledger.jsonl --json       # machine-readable
```

`status` answers, from the ledger alone: how many capsules are sealed, how
many checkpoints exist and each one's ladder rung (`self-attested` /
`witnessed` — the checkpoint's EFFECTIVE grade, original registration plus
any later backfill; see "Witness outage" below), and two honest lag
numbers — **records awaiting the next checkpoint** (sealed capsules, and a
checkpoint's own not-yet-covered stamp entry, past the latest checkpoint's
covered leaf count) and **checkpoints awaiting a witness stamp** (checkpoints
no configured witness has confirmed yet, originally or via backfill). A
third field, **`witness_backlog`**, breaks the second number down per
currently-configured witness (`--witness-url`, repeatable; defaults to
`CAPSULE_WITNESS_URL`) — how many checkpoints *that specific witness* has
not yet confirmed, so a multi-witness deployment can see that one witness
being down never hid the others having already advanced.

This is the CLI's read-verb family (`log`, `status`, `show`, `bundle`,
`disclose`, `verify`): **reads never write.** Unless `--offline` is given,
`status` makes exactly one kind of network call — a read-only GET of a
Transparency Service's public key (`capsule_emit.checkpoint
.verify_receipt_offline`) to independently re-confirm a witness receipt the
ledger *already* holds for its latest checkpoint. It never re-registers a
self-attested checkpoint to try to obtain a new stamp — that would create a
new Transparency Service log entry, i.e. a write, and `push` (which forces
a *new* checkpoint), not `status`, is where writes belong. `--offline`
skips even the read-only re-check and reports only what the ledger already
records. So does `CAPSULE_WITNESS=off` (the kill switch, see
[Kill switch scope](#kill-switch-scope-o16-03) above) — a witness-disabled
process reports each witness as `unconfirmed (witness disabled)` and never
attempts the GET, whether or not `--offline` was also given.

## Witness outage: durable retry, not a drop (O5)

Witnessing is default-on, so **outage handling is launch behavior, not an
edge case.** When a configured witness is unreachable, the checkpoint it
would have stamped is not lost, not silently dropped, and not stuck: it is
already persisted as a self-attested log entry (see "Checkpoint/stamp
persistence" above), and `capsule_emit.witness` retries it — per witness,
independently — the moment that witness comes back.

**There is no separate queue file.** The durable "queue" is the ledger
itself: every checkpoint stamp on disk that a given witness has not yet
confirmed IS that witness's pending backlog, computed fresh on demand
(`witness.checkpoint_witness_backlog(ledger_path, urls)`). That means:

- **Nothing is lost on restart.** A pending checkpoint's evidence lives in
  the ledger, not in a process's memory — killing the process mid-outage
  and starting a fresh one changes nothing about what still needs a stamp.
- **No unbounded in-memory growth.** Nothing accumulates in a Python
  list across calls; each retry pass is a read over the ledger that's
  already there for other reasons (the same full-rescan precedent
  `MmrLedger.sync()` already sets).
- **A late stamp can't rewrite history**, so it is recorded as its own
  small `checkpoint_witness_backfill` ledger entry citing the original
  checkpoint's `entry_digest` — never a mutation of the original stamp.

**Per-witness cursors.** With more than one witness configured,
`witness.retry_pending_witness_stamps(ledger_path, ts_url=...)` drains each
one's backlog independently: one witness still down stops (at its own
first failure this call) without touching another witness's already-clear
backlog or blocking its drain. The next call — whether the next real
`seal()`/`emit()` crossing cadence, or another explicit call — re-derives
the same backlog from the ledger and resumes at the same point; there is no
cursor to lose or desync.

**When it runs.** `_build_and_register` (the same fire-and-forget worker
that builds and registers each newly-due checkpoint) calls
`retry_pending_witness_stamps` first, before handling the checkpoint due
this cycle — so the backlog drains automatically on the next real write
after a witness returns, matching this module's existing "no background
timer, only real writes drive network activity" design. An operator who
wants to force a drain without waiting on the next `emit()` can call
`retry_pending_witness_stamps` directly.

```python
from capsule_emit import witness

backlog = witness.checkpoint_witness_backlog(ledger_path, ["https://witness.example"])
witness.retry_pending_witness_stamps(ledger_path, ts_url="https://witness.example")
```

## Bundle — the hand-to-anyone artifact (O16 audit item 14)

The verification chain above (`emit.py`'s module docstring) is four
separate, caller-composed primitives — inclusion, checkpoint signature, TS
receipt, rollback/consistency. `capsule_emit.bundle.bundle()` assembles all
of them, plus the record's own receipt and the *prior* checkpoint's
consistency proof, into one standalone object for a single record — the
frozen surface's §2.5 shape:

```python
from capsule_emit.bundle import bundle, verify_bundle

b = bundle("ledger.jsonl", capsule_id)   # or an unambiguous >=8-char prefix
ok, errors = verify_bundle(b)            # pure, offline, never raises
```

`Bundle` carries:

- `receipt` — the record itself, as persisted.
- `inclusion_proof` — proves the receipt is a leaf under `checkpoint`'s root.
- `checkpoint` — the *covering* checkpoint (the first one whose `mmr_size`
  reaches this record), carrying whatever witness stamp(s) it collected in
  `checkpoint.witnesses`.
- `prior_checkpoint` / `consistency_proof` — the checkpoint immediately
  before `checkpoint` for this log, and the proof that `checkpoint`'s root
  genuinely extends it. This is the bracket's *lower* bound: the record
  wasn't yet in `prior_checkpoint`. Both are `None` together, and only when
  `checkpoint.prev_size == 0` — the covering checkpoint is the log's first,
  so there is no earlier checkpoint to bound against; that is the honest
  edge case, not a gap.

A record only becomes bundle-able once some checkpoint's `mmr_size` reaches
it — `bundle()` raises `BundleError` for a record still awaiting its first
checkpoint (see `capsule_emit.status` for a read-only way to check that lag
first). `bundle()` never caches anything: every call re-reads the ledger and
re-derives the MMR fresh, so a bundle can be built by a completely different
process than the one that sealed the record, at any later time, as long as
the log still retains it.

`verify_bundle()` checks every link the two-sided append bracket depends on
— inclusion, both checkpoints' signatures (via
`checkpoint.verify_checkpoint_signature_offline`, which needs only the
checkpoint's own `key_id`, never a private key or a live `Signer` — this is
what makes bundle verification possible for a stranger at all), the
`prev_size`/`prev_root` linkage, and the consistency proof — all entirely
offline. A passing consistency check is labelled for exactly what it proves:
`"history intact between checkpoints N and M"` (anti-**rewrite**, i.e. the
history *within this bundle* wasn't reordered/truncated) — never "no fork" /
"not equivocated", since one offline bundle can never rule out a divergent
history it doesn't see; that guarantee is the witness's and multi-witness
config's job. It also checks witness-stamp authenticity
(`checkpoint.verify_witness_stamp_offline` per `WitnessRecord`, per
[stamp-authenticity-on-read-not-presence]): a stamp from the pinned default
witness (`DEFAULT_TS_PUBLIC_KEY_PEM`) is signature-verified with no network
call and no caller setup; a stamp from any other Transparency Service, with
no caller-supplied `ts_pubkey_pem`, verifies as a genuine receipt *shape*
only and does not confer full trust — a checkpoint that claims witness
stamps but has none that verify at all is fatal (`ok=False`).

`Bundle.to_dict()` / `Bundle.from_dict()` round-trip through plain JSON —
the point of "standalone": a bundle survives being written to a file and
handed to someone else's process.

### `checkpoint_cose` — the COSE_Sign1 wire form ([cll-checkpoint-cose-wire])

`Bundle.checkpoint_cose` carries the covering checkpoint as a COSE_Sign1
statement over a CBOR claims map, built once at production time (in
`witness._build_and_register`, the only place the signing key is actually
available) and carried through unchanged from there on — `bundle()` never
re-signs anything. `None` for a bundle built from a ledger predating this
field, or if the COSE serialization failed at production time (best-effort;
never blocks the JSON checkpoint path).

Where a JSON `CheckpointRecord` uses dev-ergonomic field names,
`checkpoint_cose`'s CBOR claims use the CLL I-D's own §3 spec names —
`log_size`/`commitment`/`prev_commitment`/`issued_at` instead of
`mmr_size`/`root`/`prev_root`/`timestamp`, `log_id` moved onto the signed
CWT `iss` header, `key_id` onto the COSE `kid` header. The full dev↔I-D
field-mapping table lives in
`capsule_emit/checkpoint/cose_wire.py`'s module docstring (this is the
[cll-id-field-mapping-doc] resolution: ship the mapping table, don't rename
`CheckpointRecord`'s own fields).

If the checkpoint has a prior, the claims ALSO carry a real MMR consistency
(extension) proof — not just the `prev_size`/`prev_commitment` fields —
because those fields alone are exactly as trustable as any other
self-reported string: `verify_checkpoint_cose_offline` independently
recomputes whether the claimed `prev_commitment` peaks actually bag up to
the claimed `commitment`, and rejects a checkpoint whose continuity is
merely asserted, not proven (same anti-REWRITE-not-anti-FORK honesty as
`verify_bundle`'s own consistency check above).

```python
from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline

b = bundle("ledger.jsonl", capsule_id)
if b.checkpoint_cose is not None:
    result = verify_checkpoint_cose_offline(b.checkpoint_cose)   # no capsule-emit trust needed
    assert result.ok, result.errors
```

`verify_bundle()` performs this same check automatically when
`checkpoint_cose` is present, cross-checking the decoded fields against
`Bundle.checkpoint` and failing the bundle if they disagree; absence is
never fatal.

## Disclose — bundle's conscious sibling (O16 audit item 10)

`bundle` above is always safe — digests only, no producer decision needed.
`capsule-emit disclose` is the deliberate, recorded act of handing
**bundle plus content** to a named audience:

```bash
capsule-emit disclose ledger.jsonl <id|range> --audience auditor \
  --reveal <id>:agent_input=input.json --reveal <id>:agent_output=output.json
```

```python
from capsule_emit.disclose import disclose, verify_disclosure

d = disclose(
    "ledger.jsonl", capsule_id, audience="auditor",
    reveal={capsule_id: {"agent_input": payload_in, "agent_output": payload_out}},
)
ok, errors = verify_disclosure(d)   # pure, offline, never raises
```

`<id|range>` selects records three ways: a single `capsule_id` (or an
unambiguous >=8-char prefix); `id1..id2`, a contiguous range inclusive in
ledger order; or `id1,id2,...`, an explicit list. The first two always
produce the honest `"contiguous"` completeness mode; the explicit-list form
always produces `"producer-selected"`, regardless of whether the ids happen
to be contiguous, because the caller chose to enumerate rather than bound a
range — a completeness statement can never quietly overstate what was
disclosed.

`--payloads all` (the default) requires a supplied payload
(`--reveal id:field=payload.json`) for every disclosure-eligible field
(`agent_input`/`agent_output`) that has a committed digest on every
selected record, unless the field is named in `--suppress` — this is the
equivocation-honesty rule: a partial disclosure can never masquerade as a
complete one. `--payloads selected` discloses exactly the payloads
supplied, nothing implied either way. `--suppress FIELD` withholds a field
for this audience even when a payload is available, and is recorded on the
disclosure record rather than silently dropped — suppression is a per-call
choice, never a default.

**The act seals its own receipt.** `disclose()` builds one
`capsule_emit.disclosure.build_disclosure_envelope` per selected record
(the existing single-capsule payload primitive — see `disclosure.py`), then
appends a **disclosure record** — who disclosed what range to which
audience, when — to the SAME ledger, signed with the same producer
`Signer` `seal()` uses (`kind: "disclosure_record"`,
`capsule_emit.ledger.DISCLOSURE_RECORD_KIND`). It becomes an MMR leaf like
any other entry the next time a checkpoint covers it: disclosures are
receipts too, and the audit trail of showing evidence is part of the
history. It is excluded from `read_ledger()` (and therefore `ledger
view`/`show`/`verify`) and from `bundle()`'s own record resolution — it is
the log's bookkeeping, never a bundle target itself.

**Viewing is not disclosing.** Reading your own ledger — `ledger show`,
`status` — mints no receipt; only a `disclose()` call, when content is
about to cross a boundary to another party, does.

`Disclosure.to_dict()` / `Disclosure.from_dict()` round-trip through plain
JSON, same as `Bundle`. `verify_disclosure()` checks the disclosure
record's own signature, that it agrees with the `Disclosure` object, every
bundle it carries, and — for every disclosed payload field — that it
recomputes to the digest committed on that record's receipt: a tampered
payload names itself the same way a tampered bundle does.

## Registration is opt-in, always — for direct/manual use of this API

`CheckpointConfig.ts_urls` defaults to an **empty list** — nothing is
registered anywhere until you set one. (This is the manual API described in
this section; `capsule_emit.core.emit()`'s own default path above does not
use `CheckpointConfig` — it resolves its endpoint the same way the anchor
does, via `witness_url=` / `CAPSULE_WITNESS_URL`.) The free public-good
witness tier at `witness.agentactioncapsule.org` (`DEFAULT_TS_URL` —
currently served via `anchor.agentactioncapsule.org`, CNAME pending) is
documented and
available, but a generated config shows it **commented out**
(`emit.EXAMPLE_CONFIG_TOML`), so opting in is an explicit uncomment. Any
conforming SCITT Transparency Service can be substituted — nothing here is
tied to one operator.

```python
from capsule_emit.checkpoint import CheckpointConfig, due_for_checkpoint, lag_exceeded

cfg = CheckpointConfig(cadence_entries=100, cadence_seconds=900, max_lag_entries=200)
# cfg.ts_urls == [] until you set it — e.g. cfg.ts_urls = [DEFAULT_TS_URL]

due_for_checkpoint(cfg, entries_since_last=3, seconds_since_last=920)  # True: age leg
due_for_checkpoint(cfg, entries_since_last=0, seconds_since_last=920)  # False: no unwitnessed work
```

Cadence and scheduling are yours: `due_for_checkpoint`/`lag_exceeded` are
pure helpers over your own counter — this package never runs a timer or a
cron of its own (no timing-jitter, no scheduling as a service).
`due_for_checkpoint` takes an optional `seconds_since_last` for the age leg
("100 entries or 15 minutes, whichever first") — pass the time since the
*first* unwitnessed entry, not since your last poll. Omitting it (or passing
`entries_since_last=0`) falls back to the entry-count leg alone: the age leg
never fires when there's no unwitnessed work, matching the default `emit()`
wiring's idle-silence guarantee above.

## Minimal example

```python
from capsule_emit.checkpoint import MmrLedger, emit_checkpoint

class MySigner:
    def __init__(self, key_id, secret):
        self.key_id = key_id
        self._secret = secret
    def sign(self, digest_hex: str) -> str:
        import hashlib, hmac
        return hmac.new(self._secret, digest_hex.encode(), hashlib.sha256).hexdigest()

mmr = MmrLedger(my_log)          # my_log: your own append-only capsule log
mmr.sync()                        # or append() through mmr directly
cp = emit_checkpoint(mmr, MySigner("node-a", b"..."), log_id="my-log")
# cp.root, cp.mmr_size, cp.signature -- ready to store or register.
```

## Provenance

Ported from `capsule-ledger`'s `capsule_ledger/mmr/{core,index,store}.py`
per Amendment E (2026-08-21): the CLL core is substrate a counterparty needs
in order to verify a log, so it lives in the neutral producer library rather
than forked per consumer. `capsule-ledger` consumes this package through its
public interface — see its own docs for the ledger-specific wiring.
