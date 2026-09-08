# A2A Task Evidence — reference implementation

**Independent proposal; not an official A2A extension.** Reference
implementation of the `a2a-task-evidence/v1` extension (v3.1 discussion
draft). See [`../../docs/a2a-extension/README.md`](../../docs/a2a-extension/README.md)
for the doc index, the threat-model sketch, and the draft (unposted)
composition comments.

Models `examples/a2a-ap2/boundary-seal`'s a2a-sdk shape, but implements the
v3.1 negotiate-before-acting / evidence-on-completion flow instead of the
older per-record anchor pattern.

## What's here

| File | Role |
|---|---|
| `evidence_extension.py` | Builds the §12 Task Evidence payload, seals it via `capsule_emit.seal(require_witness=True)`, assembles the §11 manifest and §9 `TaskEvidenceReference`. |
| `verifier.py` | Independent reader: the ten §13 properties, each `PASS`/`FAIL`/`NOT_PRESENT`/`NOT_CHECKED`/`INCONCLUSIVE`. Never an aggregate `trusted: true`. |
| `server.py` | a2a-sdk `AgentExecutor` + FastAPI app: advertises the extension, negotiates the requirement, records evidence, attaches the terminal reference + Artifact. |
| `client.py` | a2a-sdk client: activates the extension, sends `REQUIRED`, checks explicit acceptance, polls to terminal, fetches the bundle via `GetTask`. |
| `local_ts.py` | Hermetic loopback Transparency Service double — **not independent**, see below. |
| `run_demo.py` | Runs the full flow once and writes `vectors/positive_vector.json`. |
| `make_tamper_vector.py` | Derives `vectors/tamper_negative_vector.json` from the positive vector by flipping one byte in the presented payload. |
| `test_flow.py` | Acceptance tests, including the mutant-provability check for the tamper isolation. |
| `vectors/` | The committed positive + tamper-negative vectors. |

## Why a pinned venv

`a2a-sdk==1.1.1` requires `protobuf==6.33.6` — under `protobuf>=7` its
`proto_utils.py` reads a `FieldDescriptor.label` attribute the `upb` backend
no longer exposes, and every request fails with
`AttributeError: 'FieldDescriptor' object has no attribute 'label'`. This
reproduces against the pre-existing `examples/a2a-ap2/boundary-seal` server
too — it is not specific to this new code. The repo's main dependency set
does not pin `protobuf`, so this example carries its own
`requirements.txt`, is NOT collected by the repo's main `pytest` run
(`testpaths = ["tests"]` in `pyproject.toml`), and is verified independently
in its own pinned virtualenv — the same pattern
`examples/a2a-ap2/boundary-seal/server/requirements.txt` already uses.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r examples/a2a-task-evidence/requirements.txt -e .

python examples/a2a-task-evidence/run_demo.py            # writes vectors/positive_vector.json
python examples/a2a-task-evidence/make_tamper_vector.py   # writes vectors/tamper_negative_vector.json
pytest examples/a2a-task-evidence/test_flow.py -v
```

Everything is offline and deterministic — `local_ts.py` never leaves
loopback, and there is no dependency on `witness.agentactioncapsule.org` or
any other live service.

## What `local_ts.py` is not

It is a real Transparency Service implementation (real COSE-wire checkpoint
verification, a real single-leaf COSE Receipt minted with a freshly
generated, throwaway Ed25519 key) — but it runs in the same process as the
"server," so it proves nothing about independence. Per the proposal's own
§16 acceptance item 7, a same-operator registration is labeled
producer-operated and must never be promoted to independent continuity.
Every place `EVIDENCE_STATUS_EXTERNALLY_REGISTERED` appears in this
directory's output means "registered with this loopback double," not "seen
by an independent third party."

## The ten §13 properties, as this reference implementation reports them

On the committed positive vector:

| Property | State | Why |
|---|---|---|
| `contentBinding` | PASS | recomputed digest of the presented payload matches the claimed `evidenceId` |
| `producerSignature` | PASS | the producer key signed the carried `capsule_id` |
| `taskBinding` | PASS | payload's authority/task/context/terminal-state/artifacts match the verifier's own observed Task |
| `localInclusion` | PASS | MMR inclusion proof verifies against the checkpoint root |
| `checkpointSignature` | PASS | checkpoint signature verifies offline under its own `key_id` |
| `externalRegistration` | PASS | a witness stamp from the (non-independent) local TS verifies offline against its pinned key |
| `continuity` | NOT_PRESENT | this base profile carries no continuity assertion (optional, §10 item 8) |
| `identityAuthorityBinding` | PASS | the signer key is on the verifier's configured allowlist for the claimed authority |
| `captureCoverage` | INCONCLUSIVE | one record can never prove every eligible interaction under its capture policy was captured — this is deliberate honesty, not a limitation (§13) |
| `outcomeCorroboration` | NOT_PRESENT | no independent outcome confirmation is collected (out of scope, §15) |

On the tamper-negative vector (one byte flipped in the presented payload,
everything else byte-for-byte unchanged): `contentBinding` flips to `FAIL`;
every other property is unchanged from the positive vector — proving the
ten properties are genuinely orthogonal, not one aggregate check in
disguise.
