# Changelog

All notable changes to `capsule-emit` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

## [0.3.2] — 2026-07-13

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

### Notes
- **Floats remain tolerated at `emit()` (backward-compatible).** JCS cannot digest a
  raw JSON float (§5.1), so for float-bearing inputs `_digest` falls back to the legacy
  sorted-key encoding rather than raising — existing float-emitting callers are
  unaffected. Such an input is a known non-verifiable case (its digest is not JCS and
  will not match `verify_input_digest`) until monetary/quantity values are encoded as
  exact decimal strings. A future major version may reject floats outright.

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
