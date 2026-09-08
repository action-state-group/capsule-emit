# Threat-model sketch — `a2a-task-evidence/v1`

Independent proposal; not an official A2A extension. This sketch maps every
item in the proposal's §14 list
(`a2a-verifiable-task-evidence-proposal-v3-2026-09-04.md`) to what the
reference implementation in `examples/a2a-task-evidence/` actually does
about it today, and names what is explicitly deferred. It is a sketch, not
a completed security review — see "What this is not" at the end.

Status vocabulary used below:

- **Addressed** — the reference implementation has a concrete mechanism and
  a test exercising it.
- **Partially addressed** — a mechanism exists but is narrower than the
  full threat, or is exercised only informally (documented here, not in a
  test).
- **Deferred** — out of scope for v3.1 per the proposal's own §15, or not
  yet implemented; the risk is real and unmitigated in this codebase.

| # | §14 item | Status | Notes |
|---|---|---|---|
| 1 | Unsupported-extension downgrade and stale Agent Cards | Partially addressed | `test_unsupported_extension_request_is_observable` proves the server never attaches evidence data when the client didn't activate the extension. The proposal's stronger requirement — a client must not send a consequential `REQUIRED` request unless the *current* Agent Card advertises the exact URI — is a client-side policy this reference client does not implement (it always assumes activation succeeds); a caller relying on this code for a real deployment must add that check itself. |
| 2 | Evidence-required requests that receive direct Messages | Addressed | `client.py`'s `run_flow` raises `ExtensionRejectedError` if `send_message` never returns a Task at all (§7 `requireTask`). Not exercised by an automated negative test in this pass — the reference server always creates a Task, so there is no code path that reliably produces a direct-Message response to test against. |
| 3 | Cross-tenant Task or proof substitution | Partially addressed | `taskBinding` in `verifier.py` compares the payload's `authority`/`taskId`/`contextId`/`terminalState` against the verifier's OWN independently-observed Task (never fields read back out of the same bundle) — a substituted bundle for a different Task fails `taskBinding`. `tenantId` is carried in the payload (§12) but this reference implementation runs single-tenant, so no test exercises a cross-tenant mismatch. |
| 4 | Task, Message, and Artifact ID reuse or ambiguity across authorities | Deferred | `identityAuthorityBinding` checks the signer key against a configured `authority` string, which bounds this somewhat, but no test constructs two authorities with colliding IDs. |
| 5 | Omitted or reordered multi-turn Messages | Partially addressed | Each transcript entry carries an explicit `sequence` number and a `partCommitment` digest (§12), so a verifier COULD detect a gap or reorder by checking sequence contiguity against an independently-held Message log — this reference implementation only ever produces and verifies single-Message exchanges, so that check is not implemented or tested here. |
| 6 | Mutable URL content and incomplete streamed Artifacts | Deferred | This reference implementation never uses `Part.url`; the response Artifact is an inline `text` Part and the evidence bundle is an inline `raw` Part. §12's `NOT_RESOLVED` handling for `Part.url` content is unimplemented. |
| 7 | Replay or transplantation of an evidence reference | Addressed | `taskBinding` (mismatch #3 above) is exactly the transplantation defense: an evidence bundle produced for Task A, presented against Task B's `GetTask` response, fails because the payload's `taskId`/`contextId` won't match what the verifier observed for B. `test_tamper_flips_contentBinding_and_only_contentBinding` demonstrates the adjacent case (payload substitution under a fixed reference) directly. |
| 8 | Stale checkpoints presented as current | Deferred | `localInclusion`/`checkpointSignature` prove the checkpoint is internally valid and the record is included, but nothing here proves the checkpoint is the LATEST for its log — an equivocating producer could hand different relying parties different checkpoints. Per `docs/checkpoint.md`, that guarantee is the multi-witness / independent-continuity tier's job, which `continuity` (always `NOT_PRESENT` in this base profile) deliberately does not claim. |
| 9 | Equivocation across witnesses or relying parties | Deferred, by design | Same boundary as above: `local_ts.py` is explicitly a same-operator, non-independent Transparency Service double (see its module docstring and `README.md`'s "What `local-ts` is not" section) — it cannot detect or prevent equivocation, and the reference implementation never claims it can. `externalRegistration=PASS` proves registration happened with the configured TS, nothing about that TS's independence. |
| 10 | Selective capture and boundary bypass | Addressed by design of the property, not by mechanism | `captureCoverage` is reported `INCONCLUSIVE` even in the fully-passing positive vector, precisely because one record cannot prove that every eligible interaction under its declared `capturePolicyId` was actually captured (§13's own example). This is the correct honest answer, not a mitigation. |
| 11 | False claims by an otherwise valid signer | Partially addressed | `producerSignature` proves the KEY signed the ID; it says nothing about whether the signer told the truth about what happened. `outcomeCorroboration` (always `NOT_PRESENT`) is the property that would catch this, and this reference implementation never collects it (§15: outcome truth is explicitly out of scope for v3.1). |
| 12 | Bundle-carried keys promoted incorrectly to trust anchors | Addressed | `verifier.py`'s `TrustConfig` (`ts_pubkeys`, `authorized_signers`) is passed into `verify_evidence_bundle` as a caller-supplied argument, never read from the bundle dict itself. The bundle's own `capsule["key_id"]` is cryptographic key material only; `identityAuthorityBinding` explicitly checks it against the SEPARATE `authorized_signers` mapping and reports `NOT_CHECKED` (never a false `PASS`) when no policy is configured. |
| 13 | Key compromise, rotation, historical validity, and succession | Deferred | No key-rotation or historical-validity-at-signing-time logic exists in this reference implementation; `identityAuthorityBinding` is a single point-in-time allowlist check. |
| 14 | Low-entropy digest guessing | Addressed (by construction, not by a check) | `evidenceId`/`coreManifestDigest` are SHA-256 over payloads that include a `taskId` (a UUID from `new_task_from_user_message`) and full transcript content — not low-entropy fields alone. No explicit guessing-resistance test exists. |
| 15 | Leakage of counterparties, log size, cadence, and business activity | Partially addressed | Per `docs/checkpoint.md`, the checkpoint registration itself is checkpoint-only (size, root, timestamp, key id — never capsule content) — that guarantee is capsule-emit's, inherited unchanged here. This reference implementation's evidence bundle DOES carry the plaintext Task Evidence payload (transcript digests and role, not raw text) inline in the Artifact for demo simplicity; a deployment concerned about this should move to a commit-then-disclose pattern (`capsule_emit.disclose`) instead of inline plaintext delivery. |
| 16 | Denial of service through oversized evidence bundles | Deferred | No size limits are enforced on the transcript, artifacts list, or resulting bundle in this reference implementation. |
| 17 | Retention and deletion requirements for local preimages and personal data | Deferred | Ledger retention/deletion policy is out of scope for this example; it is a deployment operational concern documented in `capsule_emit`'s own docs, not something this extension layer adds or removes. |

## What this is not

This is a sketch prepared as pre-Issue-filing prep (v3.1 §17), not a
completed, adversarially-reviewed security assessment. It exists to show
the composition is concrete enough to discuss, per the proposal's own
framing: "evidence that the composition is concrete, not prerequisites
imposed by A2A governance." A real experimental-extension review should
re-derive this table independently rather than trust it.
