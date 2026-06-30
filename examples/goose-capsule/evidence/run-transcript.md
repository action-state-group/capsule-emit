# Live Goose v1.39.0 end-to-end run — evidence transcript

**Date:** 2026-06-30  
**Goose version:** 1.39.0 (`brew install block-goose-cli`)  
**Provider:** claude-code (Claude Code session, model claude-sonnet-4-6)  
**Extension:** capsule_emit companion server (Pattern B, `python3 -m capsule_emit.server`)  
**mcp package:** 1.26.0  
**capsule-emit:** 0.1.1  

---

## 1. Environment

```
$ goose --version
 1.39.0

$ pip3 show mcp | grep Version
Version: 1.26.0

$ pip3 show capsule-emit | grep Version
Version: 0.1.1
```

## 2. Goose config — extension wired

`~/.config/goose/config.yaml`:

```yaml
extensions:
  capsule_emit:
    enabled: true
    type: stdio
    name: capsule_emit
    description: "Record + verify Agent Action Capsules"
    cmd: python3
    args: ["-m", "capsule_emit.server"]
    timeout: 30
    envs:
      CAPSULE_LEDGER: "/tmp/goose-capsules.jsonl"
      CAPSULE_OPERATOR: "acme-co"
      CAPSULE_DEVELOPER: "goose-agent@v1"
```

## 3. Live goose run command

```bash
goose run \
  --provider claude-code --model claude-sonnet-4-6 \
  --no-session --quiet \
  --with-extension "CAPSULE_LEDGER=/tmp/goose-capsules.jsonl \
    CAPSULE_OPERATOR=acme-co CAPSULE_DEVELOPER=goose-agent@v1 \
    python3 -m capsule_emit.server" \
  --no-profile \
  -t "Call the capsule_record tool with action='submit_order', \
      tool_input='{\"vendor\": \"Frobozz\", \"amount\": 1240.19, \"po_number\": \"PO-7777\"}', \
      tool_output='{\"status\": \"dispatched\", \"confirmation_ref\": \"CONF-7777\"}'"
```

## 4. Goose output

```
The `submit_order` action has been sealed as a verifiable Agent Action Capsule.

| Field             | Value                                                              |
|-------------------|--------------------------------------------------------------------|
| Capsule ID        | a1dfb7375422c656929c09b08e92ac8c65f9fb8c02694d6d0d7cca7e023e31df |
| Action            | submit_order                                                       |
| Vendor            | Frobozz                                                            |
| Amount            | $1,240.19                                                          |
| PO Number         | PO-7777                                                            |
| Confirmation Ref  | CONF-7777                                                          |
| Ledger            | /tmp/goose-capsules.jsonl                                          |

The capsule cryptographically commits the input and output together,
producing a tamper-evident record.
```

## 5. Verify (ok=True)

```bash
$ agent-action-capsule verify --store /tmp/goose-capsules.jsonl

Store-level verification of 1 capsule(s) in /tmp/goose-capsules.jsonl:
  [0] ok: True
  capsule_id (recomputed): a1dfb7375422c656929c09b08e92ac8c65f9fb8c02694d6d0d7cca7e023e31df
  derived: effect_mode=dispatched_unconfirmed attestation_mode=self_attested ledger_mode=standalone
  findings:
    - [info] (check 8) unknown_registry_value: effect.type='submit_order' is not a seeded effect.type value; informational, not rejected (§12)
```

**ok=True ✓**  
(The `unknown_registry_value` info finding is expected for an application-defined effect.type — it is not an error and does not affect validity.)

## 6. Tamper test (ok=False)

Flipped one byte in `agent_output_digest` and re-verified:

```
=== TAMPER TEST ===
tampered digest tail: 3b1201a0   (was: 3b1201ab)
ok after tamper: False
error: capsule_id_mismatch — recomputed digest != carried digest
```

**tamper → ok=False ✓**

## 7. test_goose.py — 21/21 passed

```
$ python3 -m pytest tests/test_goose.py -v

21 passed in 0.28s
```

No tests skipped (mcp v1.26.0 installed; `pytest.importorskip("mcp")` did not skip).

---

## Summary

| Check | Result |
|-------|--------|
| Goose v1.39.0 installed | ✓ |
| capsule_emit extension wired | ✓ |
| Live `goose run` seals capsule | ✓ |
| `verify ok=True` | ✓ |
| tamper → `ok=False` | ✓ |
| `test_goose.py` 21/21 green | ✓ |

The claim "Verified against real Goose v1.39.0" is substantiated.

**Sealed capsule:** `examples/goose-capsule/evidence/capsule.json`
