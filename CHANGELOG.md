# Changelog

All notable changes to `capsule-emit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

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
