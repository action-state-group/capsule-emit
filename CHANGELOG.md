# Changelog

All notable changes to `capsule-emit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

### Added
- **`permalink --with-statements`** (`capsule_emit/permalink.py`, `capsule_emit/cli.py`):
  opt-in, default OFF. `capsule-emit permalink --ledger <l> --bundle` previously built its
  fragment from `capsules.jsonl` alone — the real `COSE_Sign1` signed statements sitting
  beside it in `<ledger_dir>/signed-statements/<capsule_id>.cose` never made it into the
  artifact. `--with-statements` embeds each capsule's matching statement as a
  `signed_statement: {statement_b64, pubkey_pem}` sidecar, best-effort (capsules with no
  `.cose` file pass through unmodified; `pubkey_pem` only when a companion
  `<capsule_id>.pub.pem` is found). Shape matches — and round-trips a `pass` against —
  `scitt-cose`'s real reference verifier (`hosted_profiles/aac.py::_check_authenticity`,
  the same one the pinned `test-vectors/tamper-states/*` fixtures are tested against), not
  an invented format. Measured on a real 5-capsule bundle: fragment grows from 7,180 to
  18,784–19,676 chars (2.6–2.7x, depending on whether pubkey files are present); a
  20-capsule bundle: 29,212 → 76,292–79,864 chars — default OFF is deliberate, not a
  placeholder, per the fragment-size caution in the originating task. Known consequence,
  not addressed here (viewer/spec territory): `signed_statement` is not exempt from the
  `capsule_id` digest recompute, so an embedded capsule fails a strict Integrity re-check
  (confirmed against this repo's own `check_capsules()`) until the viewer decision on
  Authenticity ships — see `embed_signed_statements()`'s docstring. No producer in this
  codebase writes a `.pub.pem` file today, so `pubkey_pem` will be absent for any current
  demo/PoC ledger — key discovery/distribution is unaddressed, out of scope here.
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
