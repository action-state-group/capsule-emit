# Changelog

All notable changes to `capsule-emit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

### Fixed — the verify surface authenticates cryptography, not just structure ([verify-authenticates-nothing])

**What changed.** An adversarial run against `origin/main` (`_work/adv-migration-run-2026-08-24.md`)
found the offline read/verify surface authenticated almost nothing: the self-attested Ed25519
signature (`#80`) was minted on every capsule but never checked by a shipped verify path, `verify_bundle`
never content-authenticated the receipt it hands a stranger, and the `witnessed` grade was
presence-only (`len(witnesses) > 0`), so a hand-fabricated stamp laundered a self-attested checkpoint
to `witnessed` on every offline read path. Three fixes, landed together (they share one root — the
read path trusted structure over cryptography):

- **BLOCKER-1** — `capsule_emit.signing.verify_store_signed` composes `agent_action_capsule.verify_store`
  with `verify_capsule_signature`, so a key-less forgery (attacker-authored content, invented
  signature/key_id, `capsule_id` recomputed to match) reports INVALID naming the record. Wired into
  CLI `capsule-emit verify`, `ledger view`'s inline verify column, and `permalink`/`evidence`'s
  `check_capsules`.
- **HIGH-2** — `verify_bundle` now recomputes the receipt's own `capsule_id` from its content and
  checks its self-attested signature before reporting VALID — a bundle whose receipt body was
  tampered but `capsule_id` left alone is now caught.
- **HIGH-3** — `capsule_emit.checkpoint.verify_witness_stamp_offline` is a new primitive: a witness
  stamp must be bound to its checkpoint (`entry_hash` recomputed, not trusted) and decode as a
  structurally valid COSE Receipt before it counts. `CheckpointRecord.grade()` and `verify_bundle`
  both use it now — presence in `witnesses` alone no longer grades `witnessed`. Pass a pinned
  `ts_pubkey_pem` for the full identity-bound signature check on top of the structural one.

The three original attack scripts (`/tmp/atk/attack_forge_sig.py`, `attack6b.py`, `attack45.py`) are
reconstructed as permanent regression tests in `tests/test_verify_authenticates_nothing_regressions.py`.

### Fixed — fast-follow hardening on the verify surface ([verify-batch-fastfollow])

**What changed.** Manager review of the above batch found a residual gap and an honesty gap; both
close here, on the same `verify_bundle`/`grade()` surface:

- **Consistency-proof label (Decision 2).** `verify_bundle` already verified the MMR consistency
  proof bridging `prior_checkpoint` to the covering checkpoint; it now labels a pass for exactly what
  it proves — a notice reading `"history intact between checkpoints N and M"` (anti-**rewrite**) that
  never says or implies `"no fork"` / `"not equivocated"`. Anti-equivocation is the witness's and
  multi-witness config's job, never this offline check's. The first-checkpoint edge
  (`prev_size == 0`, no prior to check continuity against) gets its own honest notice too.
- **Sophisticated-forger closure.** `verify_witness_stamp_offline`'s default (no `ts_pubkey_pem`) tier
  graded `WITNESSED` on receipt *shape* alone — a forger with the public `scitt_cose.build_receipt`
  could mint a well-formed, checkpoint-bound receipt signed with a key of their own choosing and
  launder self-attested to witnessed, one sophistication level up from a garbage-bytes forgery. Fixed
  by pinning the known default witness's (`witness.agentactioncapsule.org`) Ed25519 public key
  (`DEFAULT_TS_PUBLIC_KEY_PEM`) in the default read path: a stamp whose `ts_url` matches the pinned
  default is now signature-verified for the full `WITNESSED` guarantee automatically; a stamp from any
  other TS, with no caller-supplied key, reports `"witness shape valid; TS identity unverified"` and
  grades `SELF_ATTESTED` rather than `WITNESSED`.
- **Doc note.** `CheckpointRecord.grade()`'s two rungs (self-attested/witnessed) are correct as
  shipped — `countersigned`, the ladder's third rung, is a receipt-level property from a
  counterparty/operator's countersign path, not a checkpoint-level fact, so it has no representation
  in `grade()`.

`disclose.verify_disclosure` was adjusted to only fold a bundle's fatal errors (not its non-fatal
notices) into its own result, so the new consistency/first-checkpoint notices don't flip an otherwise
valid disclosure to invalid.

### Added — flock-based one-log-one-writer locking (O16 audit item 12, "Flock locking")

**What changed.** No OS-level write coordination existed: `git grep` for
`flock|fcntl|filelock|LOCK_EX` across the tree returned zero results, and
`ledger.py`'s `_append_lock` (`threading.Lock`) only ever protected same-process
thread races — its own inline comment said so. Two OS *processes* writing to the
same ledger had no coordination beyond OS append-mode atomicity. `append_to_ledger`
now takes an OS-level `flock` (POSIX `fcntl`) on a sidecar `<ledger>.lock` file for
the duration of each append — mandatory, no opt-out. A second process that finds the
lock held fails immediately with `capsule_emit.ledger.LedgerLockedError`, naming the
holder (pid/host/timestamp read from the lock file); waiting is opt-in only
(`append_to_ledger(..., wait=True[, timeout=seconds])`), never silent, because a torn
log manufactures fork evidence (frozen surface §7d).

This is an intentional behavior change: multi-process writers against one log
directory (previously undocumented and unsafe) now get a hard, named error instead
of a silent, lucky-or-not interleave.

See `tests/test_ledger_locking.py` (includes a real two-process test — prior
concurrency coverage was thread-level only) and `docs/concurrency.md`.

### Added — `disclose` CLI verb (O16 audit item 10)

**What changed.** There was no `disclose` verb — only `disclosure.py`'s narrow
single-capsule Disclosure Envelope builder, with no `--audience`, no range, no
completeness statement, and no self-sealing disclosure record. New
`capsule_emit.disclose` module + `capsule-emit disclose <path> <id|range> --audience
NAME [--payloads all|selected] [--reveal SELECTOR:FIELD=payload.json] [--suppress
FIELD]` CLI verb: `bundle()` (O16 audit item 14) plus selected payload content (built
per record via the existing `build_disclosure_envelope`), a **completeness
statement** (`"contiguous"` for a single id/`id1..id2` range, `"producer-selected"`
for an explicit `id1,id2,...` list — never overstated), an **audience-suppression
profile** (`--suppress`, recorded rather than silently dropped), and its own
**self-sealing disclosure record** — signed with the same producer `Signer` `seal()`
uses, appended to the same ledger as a new `kind` (`capsule_emit.ledger
.DISCLOSURE_RECORD_KIND`) so it becomes an MMR leaf like any other entry
("disclosures are receipts too"). `--payloads all` (default) refuses to build an
incomplete disclosure that claims completeness (the equivocation-honesty rule);
`--payloads selected` discloses exactly what's supplied. `read_ledger()` and
`bundle()`'s own record resolution both exclude the new `disclosure_record` kind, the
same way they already exclude checkpoint-stamp entries (see `ledger
.NON_CAPSULE_KINDS`). `--claim`-driven selection is plugin sugar, out of scope for
this neutral primitive.

See `tests/test_disclose.py`, `docs/checkpoint.md`'s "Disclose" section.

### Added — `#logged @ leaf N` repr (O16 audit item 9)

**What changed.** `EmitResult.__repr__` and `capsule-emit ledger show` previously gave
no indication that a sealed capsule is already a leaf in your log, ambiently, before any
checkpoint (frozen v4 surface §2.1). `EmitResult` gains a `seq` field — this capsule's
1-indexed position in its ledger file, the same `seq` `capsule_emit.witness`'s
`_JsonlLogSource.scan` assigns per raw line (checkpoint-stamp entries counted too), so it
becomes the MMR leaf index once a checkpoint covers it. `capsule_emit.ledger.append_to_ledger`
now returns this position. Both `repr(cap)` and `ledger show`'s header render it as
`#logged @ leaf <seq>`.

See `tests/test_logged_leaf_repr.py`.

### Changed — kill switch now scopes all egress, not just checkpoint posting (O16 audit item 3)

**What changed.** `witness=False` / `CAPSULE_WITNESS=off` used to gate only checkpoint
posting — `status`'s witness-receipt re-check (O16-17) had nothing to gate it, and the
legacy anchor channel (O16-01-02) was gated solely by the separate `CAPSULE_ANCHOR`
env var, so `CAPSULE_WITNESS=off` alone left it fully live. It is now the single switch
that zeroes all three: `capsule_emit.core._emit_capsule()` ANDs the legacy anchor
channel's own on/off decision with the witness kill switch (an explicit `anchor=True` /
`CAPSULE_ANCHOR=legacy-on` no longer re-enables anchor egress when witnessing is off),
and `capsule_emit.status.compute_status()` skips its read-only witness-receipt GET
whenever the kill switch is set, independent of whether `--offline` was also passed.
**Breaking:** the (until now supported) `anchor=True, witness=False` combination no
longer dispatches the legacy anchor channel — see `tests/test_anchor_disclosure_and_capsule_anchor_env.py`,
updated accordingly.

See `tests/test_kill_switch_scope.py`, `docs/checkpoint.md`'s "Kill switch scope" section.

### Added — `status` CLI verb (O16 audit item 17, "status's fetch-fold")

**What changed.** No `status` verb existed at all (confirmed against `cli.py`'s actual
subparser list), and there was no separate `fetch` verb either. `capsule-emit status
<ledger> [--offline] [--json]` is net-new (`capsule_emit/status.py`): it reports each
checkpoint's ladder rung (`self-attested` / `witnessed`, via O16-11's `grade()`), and two
honest lag numbers — records awaiting the next checkpoint (sealed capsules, and a
checkpoint's own not-yet-covered stamp entry, past the latest checkpoint's covered leaf
count per O16-16) and checkpoints still awaiting a witness stamp (persisted checkpoints
graded `self-attested`). Unless `--offline`, it makes exactly one read-only network
call — a GET of the Transparency Service's public key to independently re-confirm the
latest checkpoint's already-held witness receipt(s) (`verify_receipt_offline`); it never
re-registers a self-attested checkpoint to obtain a new stamp, since that would write a
new TS log entry — writes belong to `push`, not to a read verb. This folds the audit's
"fetch" concept into `status` per the frozen surface's read-verb table, matching every
other read verb's "reads never write" rule.

See `tests/test_status.py` and `docs/checkpoint.md`'s "Checking status" section.

### Added — `bundle()`, the hand-to-anyone artifact (O16 audit item 14, "Bundle contents")

**What changed.** New `capsule_emit.bundle` module: `bundle(path, capsule_id)` assembles
the frozen surface's §2.5 shape for one record — receipt + inclusion proof + covering
checkpoint (with its witness stamp(s)) + prior checkpoint + consistency proof between the
two — into one standalone `Bundle` object, JSON-round-trippable via `to_dict()`/
`from_dict()`. `verify_bundle(b)` is the pure, offline, total counterpart: it checks
inclusion, both checkpoints' signatures, the `prev_size`/`prev_root` linkage, and the
consistency proof, never raising. `prior_checkpoint`/`consistency_proof` are `None`
together, and only when the covering checkpoint is the log's first (`prev_size == 0`) —
the honest edge case the two-sided append bracket has no lower bound for yet, not a gap.

Also new: `capsule_emit.checkpoint.verify_checkpoint_signature_offline` — a checkpoint
signature check using only the checkpoint's own `key_id` (Ed25519 public key, no private
key, no live `Signer`). The existing `verify_checkpoint_signature` needs a `Signer`
capable of reproducing the signature (the producer's own round-trip check); that's
useless to a stranger verifying a bundle, which is the entire reason
`o16-14-precond-checkpoint-signer` moved the default checkpoint signer to a persisted
Ed25519 identity ahead of this change.

**Out of scope (per the audit).** No CLI verb yet — `disclose` (item 10), `status`'s
render surface (item 17), and `verify`'s bundle mode are each their own item and build on
this library function, not re-planned here. Witness-stamp re-confirmation
(`checkpoint.verify_receipt_offline`) stays a separate, explicit step in `verify_bundle`
since it may need a network fetch of the TS public key.

See `tests/test_bundle.py` and `docs/checkpoint.md`'s new "Bundle" section.

### Added — checkpoint grade: self-attested / witnessed (O16 audit item 11, "Multi-witness any-of grading")

**What changed.** `CheckpointRecord` gains a `grade()` method and a new `Grade` enum
(`capsule_emit.checkpoint.Grade`, also exported from `capsule_emit.checkpoint.emit`):
`Grade.SELF_ATTESTED` until at least one witness stamp lands, `Grade.WITNESSED`
afterward. Fan-out-to-all and per-endpoint failure isolation already worked (multiple
`witness_url`s each get an independent registration attempt — one endpoint failing never
blocks the others); what was missing was any concept of "grade" at all —
`git grep grade` in `capsule_emit/` previously returned nothing. The transition is
any-of, not all-of (frozen v4 surface §2a.3): the first valid stamp already flips the
grade, and additional independently-operated witnesses only ever compound independence,
never gate it further. A "valid stamp" here is any `WitnessRecord` present on
`cp.witnesses` — `capsule_emit.witness._build_and_register` only ever appends one after
`register_checkpoint` returns without raising, so presence already means a successful
registration.

**Out of scope (per the audit).** Per-witness cursor/queue state (strict in-order
draining) is O5/O13's scope, not re-planned here. The ladder's third rung,
`countersigned`, is a distinct mechanism (a counterparty/operator receipt citing this
one) with no representation in `Grade`. Wiring `grade()` into a render surface
(`status`, `show`, `bundle`, `disclose`) is each of those verbs' own item once they
exist — this change is additive and does not call `grade()` from anywhere yet.

See `tests/checkpoint/test_checkpoint_emit.py`'s grade tests and
`tests/test_witness_multi_and_notice.py::test_one_valid_stamp_grades_witnessed_even_if_another_endpoint_fails`.

### Added — age-based checkpoint cadence + explicit idle-silence guarantee (O16 audit item 5, "Idle silence")

**What changed.** `capsule_emit.witness.maybe_checkpoint` (the default `emit()` wiring)
and `capsule_emit.checkpoint.emit.due_for_checkpoint` (the manual/direct API) previously
only ever came due on an entry-count cadence (`cadence_entries`, default 100) — there was
no age-based leg at all, contrary to the frozen v4 surface's "Cadence: 100 entries or 15
minutes, whichever first, both configurable." Both now also come due once
`cadence_seconds` (`CheckpointConfig.cadence_seconds` / new `CAPSULE_WITNESS_CADENCE_SECONDS`
env var, default 900 — 15 minutes) has elapsed since the first unwitnessed entry after the
last checkpoint, whichever leg fires first.

**Idle silence, explicitly.** The age leg only ever fires when there is at least one
unwitnessed entry — `due_for_checkpoint` returns `False` outright when
`entries_since_last <= 0`, regardless of `seconds_since_last`, and
`witness.maybe_checkpoint`'s age clock (`witness._armed_at`) is only ever read from
inside a call that itself only runs on the back of a real `emit()`. There is no
background timer or polling thread, so an idle log (no new `emit()` calls) never comes
due on age alone — this is structural, not a runtime guard. Checkpoint-stamp entries
(O16 audit item 16) reinforce this: they are written directly through
`ledger.append_to_ledger`, never through `core.emit()`, so persisting a stamp neither
advances the entry counter nor resets/starts the age clock.

**Compatibility.** `due_for_checkpoint`'s new `seconds_since_last` parameter is
keyword-only and defaults to `None` (entry-count-only behavior, matching the prior
signature exactly). `CheckpointConfig.cadence_seconds` defaults to 900 for
constructors/`from_dict()` that don't set it.

See `tests/test_witness_idle_silence_and_age_cadence.py` and `docs/checkpoint.md`.

### Changed — checkpoint/witness stamps are now persisted ledger entries (O16 audit item 16, "Stamp-as-log-entry")

**What changed.** Previously, once `capsule_emit.witness`'s default checkpoint/witness
wiring built and registered a checkpoint, the returned `WitnessRecord`(s) were attached
only via `cp.witnesses.append(...)` — an in-memory mutation of a `CheckpointRecord` that
was never itself written anywhere; a process restart discarded it. Now every checkpoint
`capsule_emit.witness` builds (witnessed or, if every endpoint failed, still
self-attested) is written back into the *same ledger it covers* as its own JSONL entry —
`{"kind": "checkpoint_stamp", "v": 1, "capsule_id": <checkpoint.entry_digest()>,
"checkpoint": {...}}` — through the same `capsule_emit.ledger.append_to_ledger` every
capsule already goes through. That entry becomes an MMR leaf the *next* checkpoint's
`mmr.sync()` folds in, so checkpoint N's stamp is genuinely covered by checkpoint N+1,
matching the frozen v4 surface's §2.3: "the stamp does land as its own log entry ...
checkpoint N's stamp is covered by checkpoint N+1."

**Leaf coverage (PM gate ruling 2026-08-24).** The leaf commits to the checkpoint entry
AS PERSISTED — signature and witnesses included — via the new
`CheckpointRecord.entry_digest()`, not `CheckpointRecord.digest()` (the signing-body-only
value registered with the TS, unchanged for that purpose). Flipping or deleting a byte of
a persisted stamp's `witnesses` now changes its leaf and breaks the covering checkpoint's
root; see `test_tampering_with_persisted_stamp_witnesses_breaks_inclusion_under_the_covering_root`.

**Read-path compatibility.** `capsule_emit.ledger.read_ledger` filters checkpoint-stamp
entries out by default, so every existing capsule-only consumer (the CLI, `ledger.view` /
`view_chains` / `show`, `server`, `permalink`, `approval`, `holds`) keeps seeing exactly
the capsule stream it always has. `capsule_emit.ledger.read_ledger_entries` returns the
raw file, stamps included — `capsule_emit.witness._JsonlLogSource.scan` now uses this so
the checkpoint layer's own MMR indexes stamp entries as leaves too.

**Foundational.** Audit items 5 (idle-silence's stamp-exclusion leg), 11 (multi-witness
grading), 14 (`bundle`), and 17 (`status`) depend on stamp entries existing in the log at
all. Stamp entries never advance `witness.maybe_checkpoint`'s cadence/idle counter — they
are written directly via `append_to_ledger`, not through `core.emit()`.

See `tests/test_witness_stamp_persistence.py` and `docs/checkpoint.md`'s new
"Checkpoint/stamp persistence" section.

### Changed — BREAKING (default-behavior): per-seal anchor killed as a default; single egress channel (O16-01-02)

**What changed.** The per-seal SCITT anchor submission that `seal()`/`carry()`/`compose()`
(and the deprecated `emit()`) dispatched on every call by default has been killed as a
default. The checkpoint/witness stream (above) is now the **only** default network
egress path. The anchor channel still exists, as an explicit, non-default opt-in —
pass `anchor=True`, or set `CAPSULE_ANCHOR=legacy-on` (a value deliberately distinct
from the pre-0.5.0 on-values, so an existing `CAPSULE_ANCHOR=true`/unset config does
not silently keep double-egress alive across this upgrade) — kept for one release as a
rollback path. `EmitResult.anchored` / `.anchor_status` are unchanged in shape but now
report `"skipped"` / `False` for the overwhelming majority of calls (anything that
doesn't explicitly opt in). See `docs/why-anchoring.md` and `docs/checkpoint.md`.

**Why.** The dual-channel default (anchor AND witness both firing on every call) was
an undercounted-migration gap against the frozen v4 developer surface (`seal(payload)`
promises one egress story, not two independent default network paths with overlapping
purposes). See the O16 migration audit, items 1-2.

**Rollback.** `CAPSULE_ANCHOR=legacy-on` restores the pre-0.5.0 per-seal anchor
dispatch for one release; not a code revert.

### Added — one-time stderr notice for stale `CAPSULE_ANCHOR=true`/`1`/`yes` (#81 follow-up 1)

**What changed.** The O16-01-02 flip above was documented but silent at runtime: a
pre-0.5.0 affirmative `CAPSULE_ANCHOR` value (`true`/`1`/`yes`, case-insensitive) now
resolves to single-egress with no signal that the setting stopped doing anything.
`seal()`/`carry()`/`compose()` now print a one-time stderr notice, the first time such
a value is seen, naming the value, explaining it is now a no-op, and pointing at
`CAPSULE_ANCHOR=legacy-on` to restore the old behavior. `off`/`0`/`false`/`no`/unset/
`legacy-on` never trigger it, and an explicit `anchor=` kwarg (which always wins over
the env var) suppresses it too, since nothing was silently overridden in that case.

## [0.5.0] — 2026-08-23

### Added — BREAKING (capsule shape): every `seal()`/`carry()`/`compose()` result is now cryptographically signed (O16-13, "Signer protocol seam")

**What changed.** `seal()` (`capsule_emit.core._emit_capsule`) never touched a `Signer`
at all — no cryptographic signature existed over sealed capsule content anywhere outside
the opt-in checkpoint layer, and that layer's own default signer (`witness._AutoSigner`)
is ephemeral HMAC-SHA256, not a persisted asymmetric keypair. Now every capsule carries
`signature` (an Ed25519 signature, hex) and `key_id` (the raw Ed25519 public key, hex —
verify straight from the capsule, no registry lookup) by default, signed by a new
`capsule_emit.signing.LocalKeypairSigner`: an Ed25519 keypair auto-generated on first use
and persisted to disk (mode 0600, one key per ledger path by default) so the SAME key
signs every capsule across process restarts. `capsule_id` is computed AFTER
`signature`/`key_id` are added, so it commits to them too — stripping or swapping either
changes `capsule_id`.

**No opt-out, only a choice of key.** `seal(..., signer=<your Signer>)` or
`seal(..., signing_key_path=...)` (or `CAPSULE_SIGNING_KEY_PATH`) bring your own
KMS/HSM/TPM signer or relocate the default key file; there is no way to seal an
unsigned capsule. `capsule_emit.signing.verify_capsule_signature(capsule)` checks the
signature; `LocalKeypairSigner.rotate()` generates a new keypair and returns a
`RotationRecord` binding the old key to the new one (the OLD key signs the NEW
`key_id`), the substrate for a future key-binding WHO-slot receipt.

**New required dependency.** `cryptography>=42.0` moves from the optional `checkpoint`
extra to `capsule-emit`'s base `dependencies` — signing is unconditional, so the base
install needs it. `import capsule_emit` alone still does not import `cryptography` (or
`capsule_emit.checkpoint`) — the cost lands only on the first actual `seal()` call.

**Docs.** `docs/anatomy.md` no longer claims a producer signature is absent from
`cap.capsule` by default — see its "Where 'signed' and 'receipt' come in" section for
what's still layered above `signature`/`key_id` (identity binding, the COSE_Sign1
Signed-Statement wire format).

### Changed — BREAKING (default-behavior): CLL checkpoint/witness is now default-ON (emit-witness-default-on)

**What changed.** The CLL checkpoint/witness layer (`capsule_emit.checkpoint`, shipped
opt-in in the previous release via the `cll-extract-mmr-to-capsule-emit` port) is now
wired into `capsule_emit.core.emit()`'s default path. Previously a caller had to
`import capsule_emit.checkpoint` and drive `MmrLedger` / `emit_checkpoint` /
`register_checkpoint` by hand to get a witnessed stream at all — near-zero adopters
ever did, so the differentiator never reached the market. Now every ledger
participates automatically: once it accumulates `capsule_emit.witness.DEFAULT_CADENCE_ENTRIES`
(100) entries since its last checkpoint, a signed peaks checkpoint is built and
registered with a Transparency Service with zero code change required.

**Both prior rulings hold across the flip:**
- **Digest-only (Amendment E's privacy posture).** Only the checkpoint's own SHA-256
  digest crosses the wire — no capsule content, no ledger path, no action names. Same
  posture as the already-default per-emit anchor. Registration is async,
  fire-and-forget, on a daemon thread; a cadence-crossing `emit()` call never blocks.
- **Zero cost for non-users (the code's original opt-in reason).** Activation is lazy,
  per ledger path: a caller who emits once and exits, or whose ledger never crosses the
  cadence threshold, imports nothing from `capsule_emit.checkpoint` and builds no MMR —
  `tests/test_checkpoint_layer0_cost.py` and the new
  `tests/test_witness_default_on.py::test_single_default_emit_never_imports_checkpoint_subpackage`
  /  `test_many_emits_below_cadence_still_never_import_checkpoint` assert this directly,
  in a subprocess, so a regression here cannot hide behind another test's warm
  `sys.modules`.

**Endpoint.** Defaults to `witness.agentactioncapsule.org` — semantically a
witness, not the anchor, though it's the same donated public-good tier the
anchor already uses under the hood, not the Authority (that boundary is
unchanged). **[Currently served via `anchor.agentactioncapsule.org` — the
`witness.` CNAME has not propagated yet; `register_checkpoint` dispatches the
default URL's request to the anchor host until it does, so registration keeps
working today. See `capsule_emit.checkpoint.emit._PENDING_CNAME_TARGETS`.]**
Overridable per call (`emit(..., witness_url=...)`) or globally
(`CAPSULE_WITNESS_URL`); any conforming Transparency Service is substitutable.

**Multiple witnesses.** `witness_url=` (and `CAPSULE_WITNESS_URL`) now accept
either a single endpoint or several — a list, or a comma-separated string —
fanning the same checkpoint out to each independently. One endpoint failing
never blocks the others. A single default endpoint is used unless you opt
into more.

**First-use notice.** The first time a checkpoint actually goes out over the
network for a process, `capsule-emit` prints one line to stderr, once: what's
sent (a 32-byte digest, structurally incapable of carrying capsule content),
where (the resolved endpoint(s)), and how to turn it off. It never prints
again in the same process.

**Off switch.** `emit(..., witness=False)` opts out one call/ledger; `CAPSULE_WITNESS=off`
opts out everywhere without a code change. An explicit `witness=` kwarg always wins over
the env var.

**Signing.** The default path signs checkpoints with an ephemeral, per-process HMAC key
auto-generated the first time a ledger needs one. This is sufficient for that ledger's
own rollback/consistency self-check (the checkpoint chain's own integrity), but it is
not persisted across a process restart and carries no externally-attributable identity —
a deployment that wants either should drive `capsule_emit.checkpoint`'s primitives
directly with its own `Signer`, which remains fully supported and unchanged.

**Honest scope, not overclaimed.** A single-TS default checkpoint is
**witnessed (single witness)**: it upgrades the stream from *self-attested*
to third-party-checkable — the witness vouches the records under it
**existed, in that order, and haven't been rewritten since** (existence +
order + non-deletion), **never** that their content is true. It is **not**
the *multi-witness, equivocation-resistant* tier (see
[why anchoring makes it trustworthy](docs/why-anchoring.md#be-precise-about-what-it-proves-and-doesnt)),
which specifically requires witnesses a verifier can cross-check — the same
checkpoint independently co-signed by, or registered to, more than one
independently-operated log. Registering the default checkpoint with more than
one Transparency Service (see "Multiple witnesses" above) is what climbs that
ladder; the zero-config default does not do it for you. `docs/checkpoint.md`
and `docs/why-anchoring.md` say so plainly rather than letting the feature's
own name overclaim it.

**Docs reconciled:** `capsule_emit/checkpoint/__init__.py` and
`capsule_emit/checkpoint/emit.py`'s module docstrings, `capsule_emit/witness.py`'s
module docstring, `docs/checkpoint.md` (new "Default wiring" section, manual-use
section retitled, trust-tier language rewritten), `docs/why-anchoring.md` (ladder
now names the single-witness tier the checkpoint stream reaches), and `README.md`
(new "Checkpoint — your log now proves itself" section) all now describe the
default-ON path, the witness/anchor endpoint split, and multi-witness config; the
manual/direct API (`MmrLedger`, `CheckpointConfig`, `emit_checkpoint`,
`register_checkpoint`) is unchanged and remains independently documented for callers who
want their own cadence, key, or Transparency Service.

### Fixed — LAUNCH BLOCKER: anchor had no first-run disclosure (emit-anchor-disclosure-and-endpoint-consolidation)

**The bug (found by Ethan, tested against shipped 0.4.0).** `anchor` defaults to
on, and the very first `seal()`/`carry()`/`compose()` call in a process
dispatched an async, digest-only SCITT anchor submission with **no disclosure
at all** — it only became visible as a cryptic `RuntimeWarning` (a raw
`repr(ModuleNotFoundError(...))`) when the optional `scitt_cose` dependency was
missing, and even then only for whichever anchor futures happened to still be
pending at interpreter shutdown; most vanished silently. This violated the
default-on ruling's safeguard (Amendment E §E.1.4): every default network path
must disclose before it fires. The witness/checkpoint path already got a
correctly-ordered first-checkpoint notice in the previous release
(`emit-witness-0.5.0-followup`) — the anchor path, which fires on every call
rather than lazily, had never had an equivalent.

**Fixed — one combined first-run disclosure.** Before this process's first
anchor *or* witness network attempt, `_emit_capsule()` now prints a single
notice to stderr naming both endpoints (or their env-var overrides) and both
off switches, synchronously, in the calling thread, ahead of dispatch — proven
with a mock that fails if the network call is reached before the notice has
printed (`tests/test_anchor_disclosure_and_capsule_anchor_env.py`). A call
with both paths disabled triggers no network attempt and prints nothing.

**`CAPSULE_ANCHOR` env var reconciled.** The site's setup guide has long
documented `Set CAPSULE_ANCHOR=false` to disable anchoring (Rung 1 of the
adoption ladder) — but `capsule_emit` itself never read that env var; only a
handful of example scripts did their own parsing before passing an explicit
`anchor=`. `anchor` now defaults to `None` (was `True`) and, when left at that
default, resolves via `CAPSULE_ANCHOR` (`off`/`0`/`false`/`no`,
case-insensitive — matching `CAPSULE_WITNESS`'s existing convention exactly),
defaulting to on when unset. An explicit `anchor=` kwarg always overrides the
env var. `CapsuleEmitterBase` and `MCPCapsuleEmitter` (the framework-adapter
base classes) gain the same `anchor: bool | None = None` default for
consistency.

**Missing-dependency failure honesty.** A missing `scitt_cose` (the
`agent-action-capsule[anchor]` extra) is now detected synchronously, once per
process, at the moment of the first anchor attempt — and reported as one
plain stderr line (`pip install 'agent-action-capsule[anchor]'`) instead of
the cryptic per-capsule `ModuleNotFoundError` repr the `atexit` handler used
to warn with. `EmitResult.anchor_status`/`.anchored` were already honestly
derived for this case (never reports `anchored=True` without a real
`AnchorResult`) — only the message was cryptic, not the reported status.

**Endpoint inventory (repo-boundary note).** Three hardcoded anchor-family
domains exist across the stack: `ts.agentactioncapsule.org` (the actual
SCITT-submission default in `agent_action_capsule.anchor`, a separate repo),
`anchor.agentactioncapsule.org` (the domain this repo's docs/examples and the
site document as canonical), and `witness.agentactioncapsule.org` (this
repo's own `capsule_emit.checkpoint.emit.DEFAULT_TS_URL`, already
single-sourced — CNAME onto `anchor.` pending). `ts.` and `anchor.` already
resolve to the same live service via DNS today (confirmed in
`capsule-anchor/README.md`), so this is a naming-hygiene gap, not a live bug —
but true single-sourcing requires a change in `agent-action-capsule` (a
different repo, out of this task's boundary) plus the pending CNAME (an ops
action). Flagged for a follow-up rather than reached into here. This repo's
own disclosure text never hardcodes a domain — it names the resolved
override or the env var, deferring to whichever repo owns the actual default.

### Added — BREAKING: `canonicalization_id` in `compute_attestation` (mesh-llm #1332)

Every emitted capsule now carries `compute_attestation.canonicalization_id: "jcs-n"`,
naming the digest algorithm used to compute `capsule_id` (RFC 8785 JCS over
absent-field-normalised data; SHA-256; lowercase hex).  This field is required by
mesh-llm #1332.  It is distinct from `forwarded_copy.transforms`, which is the
content-transform chain between digest domains — a different concept residing in the
mesh-sidecar block.

**Why this is BREAKING and not versioned:** No previously emitted record carries a
`canonicalization_id`.  The clean migration path — *"old records verify under the
algorithm they recorded"* — is unavailable because nothing was recorded.

**Honest handling of existing records:**

(a) **Existing ledgers are demo-grade.** `ledger-live` already carries the
    briefly-public-key provenance note; nothing of evidentiary weight depends on
    pre-#1332 digests.  The cost of breaking their re-verification is close to zero.

(b) **Retroactive mapping — labelled documented-not-recorded, an inference.**
    Records with `format_version ≤ '2'` that carry no `canonicalization_id` used
    one of two legacy conventions (`repr()` or `f"{x:.3f}"`), neither of which is
    the `jcs-n` rule.  Inferring that they used `jcs-n` would be false; the honest
    label is *"legacy repr-era, convention undeclared."*  Do not verify old digests
    against `jcs-n`; the result is undefined.

(c) **The identifier ships in this PR** — the fix and the spec obligation are one
    change, per the decision of record (2026-08-17).

**Number-rule additions (same PR):**

- `capsule_emit.numbers.float_to_str` — RFC 8785 §3.2.2.3 (ECMA-262 §7.1.12.1)
  binary-float-to-string.  Retires `repr()` and `f"{x:.3f}"` everywhere.  Rust
  side: `ryu-js` 1.0.3 (boa-dev, ECMAScript-compliant, ~28M downloads).
- `capsule_emit.numbers.CANONICALIZATION_ID = "jcs-n"` — the provisional identifier
  (two-maintainer CPB concurrence pending; field ships regardless).
- RFC 8785 Appendix B KAT set added to `tests/test_float_to_str.py` with exact
  IEEE 754 bit patterns.  The KATs catch a compliance gap in an implementing crate
  at the moment it matters; citing `ryu-js` is not the same as testing it.
- `FloatInDigestError` and `UnsafeIntegerError` now propagate immediately at
  `emit()` and `MCPCapsuleEmitter` call sites, naming the rejected field, rather
  than being silently swallowed by the adapter's fail-safe wrapper.

### Added
- **MCP toolset digest — `ext.mcp`** (`capsule_emit/adapters/mcp.py`): closes the gap NSA CSI
  *"MCP: Security Design Considerations"* (May 2026, U/OO/6030316-26) documents — a capsule
  proves what the agent did, not which tool descriptions the model was shown, so a server
  that swaps a tool's description after gaining trust was previously invisible in the record.
  `MCPCapsuleEmitter.capture_toolset(tools)` digests the tool manifest as presented to the
  model (name + description + input schema, JCS canonicalized, sorted by name) and carries it
  as a namespaced payload extension (`ext.mcp.toolset_digest` + `digest_alg` + a
  `manifest_ref` typed reference) on every capsule — the same value while the toolset is
  stable, a visible digest change between adjacent capsules on a mid-session swap.
  `emit_manifest_artifact=True` (default) also writes the canonical manifest bytes to disk as
  openable evidence on first capture and on every change; `False` withholds the bytes while
  keeping the digest visible. See `docs/extensions/mcp-toolset-digest.md` for the exact
  digest context.

### Fixed
- `_pending_anchors` (the module-level dict backing the atexit anchor-join
  handler added in 0.4.0) no longer leaks one entry per `emit()` call forever
  on the default non-blocking path. `_track_pending_anchor` now sweeps
  already-completed futures out of the dict on every new submission — correct
  for both anchor success and anchor failure, since `AnchorFuture.done()`
  goes `True` on either outcome inside `async_anchor`'s worker thread. A
  single capsule anchored right before a process goes idle (no further
  `emit()` calls to trigger a sweep) still relies on the `atexit` handler as
  the final backstop — that residual, bounded case is not fully eliminated,
  only made non-unbounded.
- `examples/mcp-capsule/demo.py` now passes `anchor_wait=10.0` so the
  flagship demo shows a genuine `anchored: True` / `anchor_status: confirmed`
  against a reachable endpoint, instead of the honest-but-discouraging
  `anchored: False` the 0.4.0 fix otherwise surfaces on every run.
  `MCPCapsuleEmitter` and `CapsuleEmitterBase` gain an `anchor_wait=` param
  threaded through to `emit()`.
- `examples/mcp-capsule/demo.py`: `submit_order`'s `amount` parameter was typed `float`,
  which made every capsule in the demo fail to seal (`FloatInDigestError`, silently
  swallowed by the adapter's fail-safe emit, so the demo crashed later on a `None` capsule).
  Now `str`, per the profile's exact-decimal-string rule for monetary values (§5.1).

### Known limitations
- **A failed anchor submission leaves no durable trace.** On the default
  non-blocking path, a background anchor failure is visible only as an
  in-memory `EmitResult` (never observed unless the caller passed
  `anchor_wait=`) or a `RuntimeWarning` printed to stderr by the `atexit`
  handler at interpreter shutdown — the ledger row itself records nothing
  about the failure. A long-running service that never inspects stderr, or a
  short-lived script whose stderr isn't captured, has no way to discover or
  retry a dropped submission after the fact. A retry queue or a sidecar
  failure log would close this gap; tracked as a follow-up, not fixed here.

## [0.4.0] — 2026-08-05

### Fixed — BREAKING: `EmitResult.anchored` is now honestly derived (capsule-emit#43)

**What `anchored` used to mean:** `True` whenever `emit(anchor=True)` was called
(the default), *regardless of whether the anchor submission ever succeeded*.
`core.py` fired the digest off to `agent_action_capsule.anchor.anchor()` (a
fire-and-forget helper that swallows every exception with no error path) and
then hardcoded `anchored = True` on the next line — unconditionally. Unreachable
endpoint, DNS failure, HTTP 500, no `AAC_ANCHOR_URL` set at all: still `True`.
This directly contradicted this same library family's own MUST — "`anchored`
is reported ONLY when a receipt actually verifies" (`agent_action_capsule`'s
`transparent.py` / `verify.py` / `cli.py`) — a conformance defect in the
flagship SDK against a rule enforced everywhere else in the same package.
**`capsule-emit` 0.3.2, published on PyPI, carries this old, incorrect
semantics** — anyone who installed `capsule-emit` before this release and
relied on `anchored=True` as evidence of a successful submission was told
something the library never verified.

**What `anchored` means now:** `True` if, and only if, a real `AnchorResult`
confirmed the submission via `agent_action_capsule.anchor.async_anchor()`
(the `AnchorFuture`/`AnchorResult`/`AnchorError` surface). The default
non-blocking anchor path — still the default — can never observe that outcome
by the time `emit()` returns, so `anchored` is now `False` in the common case.
This is correct, not a regression: the old `True` was never true.

- `EmitResult` gains `anchor_status: "confirmed" | "submitted" | "failed" |
  "skipped"` for callers who want the weaker "was it submitted" fact without
  it being confused for actual confirmation.
- `emit()` gains `anchor_wait: float | None = None` — block up to N seconds
  for the real outcome and get an honest `anchored` back synchronously.
- An `atexit` handler now joins outstanding anchor futures (bounded, shared
  timeout, default 5s, overridable via `CAPSULE_EMIT_ATEXIT_ANCHOR_TIMEOUT`)
  so a short-lived process (script, CLI, notebook, test) no longer silently
  drops an in-flight submission on exit — the exact race independently
  reported three times by @thisjody, including in a real signing ceremony.
  On timeout or failure the handler emits a `RuntimeWarning` naming the
  `capsule_id` and endpoint; a genuine success is never warned about.
- `agent-action-capsule` dependency now pins `[anchor]` (pulls in
  `scitt-cose`/`cryptography`) so the default `anchor=True` path actually
  works out of the box — it previously depended on the bare package, which
  does not include what `async_anchor()` needs.
- Swept every re-export of the old always-`True` claim onto the corrected
  semantics: `skills/openclaw/seal_server.py`'s `/seal` and `/verify`
  endpoints, `examples/mcp-capsule/demo.py`, `examples/nanda-tax-audit`
  (which never branched on `.anchored` but used "anchored" narratively to
  mean "capsule-sealed" — corrected to avoid the same ambiguity), and
  `examples/nanda-trust-capsule`'s reputation-gate docstrings (gate 3 checks
  local-ledger presence, not public-anchor confirmation — was documented as
  "the public time-anchor gate," which it was never enforcing).

**If you pin `capsule-emit` and check `.anchored`:** re-audit that check. If
you need a real synchronous confirmation, pass `anchor_wait=`. If you only
need "was a submission attempted," use `.anchor_status`. Filed and reported
by [@thisjody](https://github.com/thisjody) — thank you for the clean repro
and the accurate root-cause instinct (Fixes #43).

## [0.3.2] — 2026-07-13

### Added
- **Bilateral asymmetry — the ghost** (`capsule_emit/bilateral.py`):
  `BilateralState.COUNTERSIGN_REFUSED`, `BilateralHandshake.ghost()`
  (REQUESTED → COUNTERSIGN_REFUSED), and `seal_ghost()` (emits
  `verdict_class="countersign_refused"`, `effect.status="planned"`,
  `chain.relation="supersedes"`, chained to the request capsule). A ghost is not
  a both-assert: the honest party holds two capsules (request + ghost), the
  counterparty holds zero — the asymmetry is provable end-to-end. Three-arc demo
  (authorized, blocked, ghost) under `examples/bilateral-ghost/`.

### Fixed
- **Seal/verify digest canonicalization mismatch** (`core._digest`): `emit()` sealed
  `agent_input_digest` / `agent_output_digest` / `response_digest` with
  `json.dumps(sort_keys=True)`, while `verify_input_digest()` recomputes with RFC 8785
  (JCS). For "clean" values (all-ASCII, no null, no empty container) the two coincide,
  but for any value carrying a `null`, an empty `{}`/`[]`, or a non-ASCII field they
  diverged — so a **faithfully-sealed** input could fail `verify_input_digest()`
  (returned `False`) and any downstream anchored-receipt check would wrongly reject it.
  `_digest` now delegates to the same `json_digest` (JCS) the verifier uses, so
  seal and verify always agree. Capsule IDs for clean inputs are unchanged
  (JCS ≡ sorted-key JSON there), so this is backward-compatible for existing
  clean-receipt ledgers.

- **`verify_input_digest` never throws** (`verify.py`): per the profile's structured-result
  contract ("a verifier MUST return a structured result, never throw"), a candidate that
  cannot be JCS-canonicalized — e.g. one carrying a raw float (§5.1) — now returns `False`
  instead of propagating `FloatInDigestError`. This closes a crash/DoS surface where a single
  float-bearing receipt could abort a caller's scoring/verification loop.

### Changed (behavior)
- **Floats now fail closed at `emit()`.** A raw JSON float in `agent_input` / `agent_output`
  is a §5.1 error (it cannot be reproducibly digested), so `emit()` raises `FloatInDigestError`
  at seal time rather than silently sealing a receipt its own verifier could never confirm.
  **Encode monetary/quantity values as exact decimal strings** (or integer minor units) before
  sealing. Non-JSON-native types the legacy encoder tolerated (e.g. tuples) still fall back and
  seal. This is a behavior change from 0.3.1, which accepted floats and sealed a non-verifiable
  (non-JCS) digest.

## [0.3.0] — 2026-07-06

### Added
- **Bilateral attestation** (`capsule_emit/bilateral.py`): `BilateralHandshake` state machine,
  canonical payload functions, `seal_request`/`seal_action`/`seal_bilateral`, and a
  `dict_verifier`/`dict_signer` HMAC demo; four-move Org A ↔ Org B example under `examples/bilateral/`.
- **Engine-free ledger viewer** (`capsule_emit/viewer.py`): `render_table()` (refusal markers,
  actor lineage labels, verify column) and `render_html()` (single-file dark theme);
  `ledger view --html <path>` added to the CLI.
- **Approval record + pending-action pattern** (`capsule_emit/approval.py`): `seal_approval()`
  (approver identity, `human_disposed`, `chain.relation="resolves"`) and crash-safe `list_pending()`
  that reads only from the JSONL ledger.
- **Verified-flow wicket**: constraint → check → gate → seal, with a `constraints=` kwarg on the MCP adapter.
- **MCP flagship**: adapter hardening plus a stranger-runnable 5-minute quickstart.
- **AAuth bilateral interop example** and the **Amaury receipt pack** (`examples/amaury-receipt-pack/` —
  four sample capsules with an anchor + pyscitt verification walkthrough).

### Changed
- Pinned `agent-action-capsule>=0.1.0` (the bilateral `verify_pair`, `history`, and
  selective-disclosure modules ship in the 0.1.0 CORE).

## [0.1.1] — 2026-06-21

### Fixed
- `core.emit()` now accepts and threads `human_disposed`, `approver`, `decision`, and
  `relation` parameters — previously hardcoded, so HITL and superseding capsules were
  not expressible via `emit()`.
- `InvariantError` raised when `human_disposed=True` without `approver="human"`, and
  when `relation != "confirms"` without `confirms=<id>` — prevents silently wrong records.
- `adapters/_base.py`: `emit_capsule()` threads all four new params to `core.emit()` —
  adapter-emitted capsules now carry correct disposition and chain fields.
- `agent-action-capsule` pinned to `>=0.0.3` (0.0.2 had a digest-drop bug and no
  JSONL `--store` support; 0.0.3 is the fixed verifier).

### Added
- 100 hardening tests across producer, adapters, and interop paths (W4/W5/W8).
- `relation=` parameter on `emit()` — pass `"supersedes"` or `"escalates"` for non-confirm chains.
- Seeded vocabulary in docs: examples now use `effect.type="write_order"` (a registered
  value) so `verify` produces clean output on the tutorial path.
- "No effect block by default" note in Hermes, LangChain, CrewAI adapter docs.

## [0.1.0] — alpha

Initial public release: the producer/emission layer for the Agent Action Capsule
profile.

### Added
- `emit()` — one call to seal a content-addressed, digest-committed capsule of an agent
  action and its outcome (may/did verdict + confirmed-effect binding).
- Anchoring on by default — digest-only submission to a SCITT transparency log,
  recorded in an RFC 9162 transparency log (inclusion checkable against the log;
  surfacing the receipt onto the result is roadmap); repointable via `AAC_ANCHOR_URL`
  / `anchor_url=`, disable with `anchor=False`.
- Chaining via `confirms=` (parent linkage; `approved → executed → confirmed`).
- Layer capture: `agent_input`, `agent_output`, `model`, and compute attestation
  digest-committed into the capsule.
- Framework adapters: MCP (`@emitter.tool`), LangChain callback, CrewAI `wrap()`,
  Hermes — over one shared `CapsuleEmitterBase`.
- `manifest.md` declaration parser (declare autonomy + constraints; enforcement
  is a downstream, same-file concern).
- `capsule-emit ledger view` CLI over the local append-only JSONL ledger.
- Apache-2.0 license; neutrality CI gate; product-free substrate.

[Unreleased]: https://github.com/action-state-group/capsule-emit/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/action-state-group/capsule-emit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/action-state-group/capsule-emit/releases/tag/v0.1.0
