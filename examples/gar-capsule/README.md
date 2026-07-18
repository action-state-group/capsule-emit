# GAR Session Audit Record → SCITT Signed Statement

**Testable, not asserted.** Synthetic keys and IDs throughout; nothing here
is a production artifact. The goal is a runnable serialization path so Tom
can run the reciprocal SOOS-side verification of one capsule.

## What this is

A runnable example that constructs a synthetic GAR Session Audit Record (SAR)
per the PUBLIC `draft-sato-soos-gar-01` format, serializes it as a SCITT
Signed Statement via capsule-emit, and verifies the digest round-trip in
process.

- **GAR** = Governance Audit Record (SOOS protocol family, individual I-D by Tom Sato)
- **SAR** = Session Audit Record — the per-session audit artifact (GAR §6.2)
- **kernel-attested** = GEC (Governing Enforcement Component) originated; see RATS RFC 9334
- **KIA-signed** = signed by the GEC keypair per `draft-sato-soos-kia`
- **SCITT content type**: `application/soos.gar.sar+json` (GAR §10.1)

Format basis: PUBLIC `draft-sato-soos-gar-01` — IETF Datatracker:
<https://datatracker.ietf.org/doc/draft-sato-soos-gar/>

## Run

```bash
pip install capsule-emit agent-action-capsule
python3 demo.py --no-anchor    # offline / fully synthetic (recommended)
python3 demo.py                # with live anchor POST
```

Exit 0 on success. Output includes:

- The SAR field summary (from `sample-gar-block.json`)
- The capsule_id and SCITT Signed Statement written to `sample-scitt-statement.json`
- `agent_input_digest` — SHA-256(JCS(SAR JSON)) — the value Tom's SOOS verifier should recompute
- In-process verify result (`agent_action_capsule.verify`)
- Input digest round-trip result (`capsule_emit.verify_input_digest`)

## Files

| File | Description |
|---|---|
| `sample-gar-block.json` | Synthetic SAR JSON — standalone, no Python needed. Tom's SOOS-side verifier consumes this. |
| `sample-scitt-statement.json` | Written at runtime. The SCITT Signed Statement envelope with the capsule and digest metadata. |
| `demo.py` | Runnable demo (this file). |

## Tom's reciprocal step

1. Load `sample-gar-block.json` (plain JSON — no Python dependency on this side).
2. Compute `SHA-256(JCS(sar_block))` — must match `agent_input_digest` in `sample-scitt-statement.json`.
3. Run your SOOS-side KIA verifier against `kia_signed.signature` (the field
   is a non-functional placeholder here — substitute a real GEC keypair signature
   to complete the KIA round-trip).

## Notes

- All keys, signatures, and IDs with `synth-` prefix are non-functional placeholders.
- The `kia_signed.signature` field is a placeholder `A...A` byte string. Substitute a
  real Ed25519 GEC keypair signature to run a live KIA verification.
- "Merkle-rooted" language appears in the datatracker abstract but is **not** in the
  published draft-00/01 text. The actual integrity mechanism is Ed25519 over
  canonical JSON (lexicographic key order, no whitespace) — which is what
  `agent_input_digest` captures here.
- This is exploratory interoperability against public drafts. Not a conformance claim.
