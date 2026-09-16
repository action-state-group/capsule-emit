# Inspect sealed-log demo

A worked example of sealing an [Inspect](https://github.com/UKGovernmentBEIS/inspect_ai)
`.eval` log with `capsule_emit.adapters.inspect_ai`, registering one checkpoint
with a public witness, and building two disclosure bundles — one with every
sample's payload disclosed, one with a single sample's payload withheld.

**Before anything else, three things this demo is NOT:**

1. **No real inference.** The eval's model calls are Inspect's built-in
   `mockllm/model` provider — deterministic mock output derived from the
   question text, not a real model. No API key, no network call to any
   inference endpoint.
2. **Post-hoc sealing, not contemporaneous capture.** `seal_eval_log` reads a
   *finished* `.eval` log after Inspect has already produced it; it was never
   attached to Inspect's solver loop while the eval ran. The checkpoint
   establishes **integrity and existence from the moment of sealing** — not
   contemporaneous capture at the harness boundary while each model call
   happened. A "seal as it fires," attached-at-the-boundary mechanism is a
   different, real thing, and is the filed follow-up
   `[evaluator-inspect-live-hook-and-real-eval]` — a next step, not built here.
3. **One witness, ours.** The checkpoint below was registered with a single
   witness, `witness.agentactioncapsule.org`, operated by the same party that
   publishes the Agent Action Capsule spec. Its receipt grades `witnessed`
   (`capsule_emit`'s ladder is two rungs, `self-attested` / `witnessed` — see
   `checkpoint_receipt.json`; there is no higher "countersigned" or
   third-party-adjudicated tier here, and none is claimed). Whether one
   witness operated by the spec's publisher is independent enough for your
   purposes is for you to judge — this README doesn't assert an answer.

Everything here runs offline except that one witness registration — see
[What this does NOT establish](#what-this-does-not-establish) for the fuller
list.

## What this shows

1. **`generate_eval_log.py`** runs a tiny arithmetic task (`add(a, b)`, four
   samples) through Inspect's built-in `mockllm/model` provider — no API
   key, no network call to any model, deterministic output derived from the
   question text. Each sample makes **two** model calls (a reasoning step,
   then a final-answer step), so the resulting `.eval` log exercises the
   part of the mechanism that matters here: a sample is not one call.

2. **`run_demo.py`** reads that log with `capsule_emit.adapters.inspect_ai.seal_eval_log`,
   which seals:
   - one record per `ModelEvent` in the sample (the harness's own
     `request`/`response` bytes, digested unmodified — never reconstructed
     from a higher-level message list), chained in call order;
   - one record for the sample's terminal state (final completion + score,
     or its error), chained to the last model-call record.

   12 records come out of 4 samples (2 calls + 1 terminal each).

3. `witness.push()` forces one checkpoint over those 12 records and
   registers it with the live public witness
   (`witness.agentactioncapsule.org`) — the same witness `capsule-emit`
   registers checkpoints with by default. The receipt is saved to
   `checkpoint_receipt.json`.

4. Two bundles are built with `capsule_emit.disclose`:
   - `bundle_disclosed.json` — all 12 records, every payload revealed.
   - `bundle_withheld.json` — the *same* 12 records, but the three records
     belonging to one sample (`add-9-9`) carry no revealed payload. Their
     digests are still present and still covered by the checkpoint — the
     viewer's privilege log renders them `WITHHELD`, not blank, not failed
     (confirmed below).

5. Both bundles verify offline with `capsule_emit.disclose.verify_disclosure`
   (no network, no reader beyond this library).

6. A tamper check: one digit is flipped in a disclosed sample's response
   payload, and the same bundle is re-verified. It fails, and the error
   names the exact record:

   ```
   <capsule_id>: agent_output payload does not match its committed digest
   ```

7. `permalinks.json` contains a browser-verifier URL for each bundle
   (`https://verify.agentactioncapsule.org/v/<id>#...`) for the manual
   verification pass — see [Verifying in a browser](#verifying-in-a-browser).

Run it yourself:

```bash
pip install inspect_ai "capsule-emit[dev]"
python run_demo.py --out-dir out
```

`--no-witness` skips the live registration (useful offline; the two bundles
and the tamper check still run — a bundle just won't have a witness stamp).

## What this establishes

- **Existence no later than the checkpoint's registration time.** The
  witness's receipt is independent third-party evidence that these 12
  records, in this order, existed at the timestamp it signed — not
  self-reported by whoever ran the eval.
- **Contents unchanged since sealing, for whatever is disclosed.** A
  disclosed payload either recomputes to the digest the checkpoint covers,
  or the mismatch is reported by name (`verify_disclosure`) — never silently.
- **What was withheld, honestly.** A withheld record's digest is present and
  covered by the same checkpoint as every disclosed record; the field is
  labeled `WITHHELD`, not omitted from the bundle and not shown as if it
  never existed.
- **Per-model-call granularity inside a sample.** Because Inspect's own
  `ModelEvent.call` is what gets digested, an evaluator can cite one specific
  call inside a multi-turn sample, not just the sample's final answer.

## What this does NOT establish

- **Nothing about whether the eval itself is honest, uncontaminated, or a
  fair test.** Sealing a transcript says the transcript is what it claims to
  be; it says nothing about the transcript's content being a good measure of
  anything.
- **Nothing beyond the sealing boundary.** This adapter reads a *finished*
  `.eval` log — it was not attached to Inspect's solver loop while the eval
  ran (`capsule_emit.adapters.inspect_ai`'s module docstring says why: Inspect
  exposes no external seal-at-call hook to attach to instead). If Inspect
  itself never captured a raw request/response for some call, there is
  nothing here to digest for it either — an omission on the source side
  stays an omission here.
- **No claim about how long the `.eval` log existed before this adapter ran.**
  The checkpoint bounds *this process's* sealing time, not whenever Inspect
  originally produced the log.
- **No claim about the model provider's own record of the exchange.** This
  is the requester's (harness's) side only — see "Both halves" in the
  companion write-up on bilateral records for what a second, independently
  sealed side of the same exchange would add, which this example does not
  build.

## Cost

One HTTP call per checkpoint (a few hundred bytes: log size, a root hash, a
timestamp — never capsule content). This demo forces exactly one checkpoint
for 12 records; `capsule-emit`'s normal default cadence is every 100 records
or 15 minutes, whichever comes first, so a real deployment would checkpoint
far less often than once per eval run.

## Run your own witness

The checkpoint above went to the one witness this project currently runs.
Anyone can run another and register the same checkpoint with it too:

```bash
# self-host a witness instance (capsule-anchor is the reference Transparency Service)
pip install capsule-anchor
python -m uvicorn capsule_anchor.app:create_app --factory

# register with more than one witness at once
python -c "from capsule_emit import witness; witness.push('ledger.jsonl', ts_url=['https://witness.agentactioncapsule.org', 'https://your-witness.example'])"
```

A single default witness is a **single-witness** trust tier: it upgrades the
log from self-attested to third-party-checkable, but it is not yet the
multi-witness tier that lets a verifier cross-check independent operators
against each other (see `docs/checkpoint.md` in `capsule-emit`).

## A finding from testing this, not something this example claims

Cross-checking these bundles against the local `scitt-cose` viewer's
client-side digest recompute (`hosted_profiles/hosted.py`'s
`canonicalPayloadText`, run headlessly through `tests/js_harness_bundle.mjs`)
surfaced a real discrepancy: that function canonicalizes with
`JSON.stringify(payload, Object.keys(payload).sort())`, and the second
argument to `JSON.stringify` is a replacer *array*, which JavaScript applies
recursively — so a nested field's own keys, not being in the top-level
allowlist, get silently dropped. A genuinely-revealed `agent_input` payload
in this demo (which nests a `request` object) recomputes to the wrong digest
under that function and would render as a mismatch that isn't real. We
confirmed independently that the committed digest matches
`agent_action_capsule`'s own RFC 8785 (JCS) `json_digest()` over the exact
same payload — the capsule is correct; the viewer's recompute function is
not recursive. The flat (single-level) `agent_output` field in this same
bundle recomputes correctly, which is how the tamper check above was
confirmed via that harness. This was flagged for `scitt-cose` maintainers
and is now [scitt-cose #47](https://github.com/action-state-group/scitt-cose/pull/47)
(open, not yet merged/deployed as of this writing) — not fixed in this
change (different repo, out of scope here).

## Verifying in a browser

**Do NOT use the deployed page (`verify.agentactioncapsule.org`) for this
demo's bundles until [scitt-cose #47](https://github.com/action-state-group/scitt-cose/pull/47)
is merged and deployed.** The deployed viewer's digest recompute doesn't
recurse into nested payloads (see the finding above), and `bundle_disclosed`'s
`agent_input` field nests a `request` object — so the deployed page will
render a **false `MISMATCH`** on an otherwise-correct, untampered record.
Run the viewer locally from the `#47` branch instead:

```bash
git clone https://github.com/action-state-group/scitt-cose
cd scitt-cose && git fetch origin pull/47/head:pr-47 && git checkout pr-47
pip install -e ".[serve]"
uvicorn hosted_profiles.hosted:make_asgi_app --factory --port 8080
```

Then swap `https://verify.agentactioncapsule.org` for `http://localhost:8080`
at the start of each permalink below (the URL fragment after `#` is unchanged
either way — it never leaves the browser). Everything below assumes that
local viewer, not the deployed one:

1. Open the `bundle_disclosed_url` link. Every record should show a
   recomputed digest matching its committed one.
2. Open the `bundle_withheld_url` link. The three `add-9-9` records should
   render their `agent_input`/`agent_output` rows as `WITHHELD`, with a
   digest and no payload — not blank, not an error.
3. Take a screenshot of each.
4. Reload `bundle_disclosed_url` after editing one character inside the URL
   fragment's JSON (or use `tamper_demo.json` in the output directory,
   which already records the before/after text and the CLI's failure
   output) and confirm the same record now shows a digest mismatch.

The CLI-side equivalent of all three checks — which this script already
ran and which does not require a browser — is `verify_output.txt` and
`tamper_demo.json` in the output directory.
