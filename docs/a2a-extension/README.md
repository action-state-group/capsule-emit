# A2A Verifiable Task Evidence — extension docs index

**Independent proposal; not an official A2A extension.** Provisional
extension URI: `https://agentactioncapsule.org/extensions/a2a-task-evidence/v1`
(must resolve to the published proposal text before any demo — that
publication is a separate, site-repo task, not part of this directory).

This directory is the pre-Issue-filing prep for the v3.1 discussion draft
(`a2a-verifiable-task-evidence-proposal-v3-2026-09-04.md`, tracked outside
this public repo). It does not restate the proposal; read that document for
the normative prose. What's here:

- **[`threat-model.md`](threat-model.md)** — a sketch mapping every item in
  the proposal's §14 security list to what
  `examples/a2a-task-evidence/` actually does about it (addressed /
  partially addressed / deferred), not a completed review.
- **[`composition-comments.md`](composition-comments.md)** — draft (NOT
  POSTED) comments for four adjacent `a2aproject/A2A` threads, asking
  whether to share a composition point before filing a new Issue. An
  operator posts these; this repo never does.
- **[`../../examples/a2a-task-evidence/`](../../examples/a2a-task-evidence/)**
  — the reference implementation: an a2a-sdk client + server, a ten-property
  §13 verifier, and a positive + tamper-negative vector pair. See that
  directory's own README for how to run it.

## Scope note: what "evidenceStatus" means in this reference implementation

The reference server (`examples/a2a-task-evidence/server.py`) reports the
`evidenceStatus` it actually achieved (`SIGNED_RECORD` or
`EXTERNALLY_REGISTERED`), even when a client declared `minimumEvidence`
higher than what was achieved by the time the terminal response was sent. It
does not unilaterally relabel an honest, lower achievement as `FAILED`. The
proposal's §7/§8 text puts the burden of comparing achieved-vs-required on
the client ("the client must still verify explicit acceptance in the
response"), and this reference implementation follows that reading. A
production server that wants to enforce its OWN minimum (rather than
whatever the client asked for) would add that policy on top of this code,
not inside `seal_task_evidence()`.

## Scope note: `local_ts.py` is not independent

Every vector and demo run in this directory registers checkpoints with a
same-process, freshly-keyed Transparency Service double
(`examples/a2a-task-evidence/local_ts.py`) — never the public witness at
`witness.agentactioncapsule.org`, and never an actual third party. This is
deliberate: it keeps the demo and its vectors fully offline and
deterministic (no live-network dependency in a reproducible artifact), per
the proposal's own §16 experimental-acceptance item 7 ("a same-operator
service may establish registration under an accepted key but is labeled
producer-operated, and never promoted to independent continuity"). Anywhere
`EVIDENCE_STATUS_EXTERNALLY_REGISTERED` or `externalRegistration: PASS`
appears in this directory's output, read it as "registered with a
same-operator double, not an independently operated witness."

## Boundary

Record layer only. Nothing here defines outcomes, judging, compiling, or
pricing vocabulary; no vendor names appear in the extension text or code.
"Internet-Draft," never "RFC"; no claim of being first.
