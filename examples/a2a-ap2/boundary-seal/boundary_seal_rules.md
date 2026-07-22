# A2A ↔ AAC Boundary Seal — Construction Rules

**Machine:** action-state-group  
**a2a-sdk version:** 1.1.1 (PyPI `a2a-sdk==1.1.1`, git `86c6b0d`)  
**SDK shims:** none  
**capsule-emit version:** see `pyproject.toml`  
**Specification:** `draft-mih-scitt-agent-action-capsule-02`  

---

## 1. Fields committed by the boundary seal

The AAC capsule commits the following A2A task fields:

| Capsule field | A2A source field | Notes |
|---|---|---|
| `agent_input_digest` | `params.message.{taskId, messageId, role, parts[0].text}` | SHA-256(JCS(normalize(agent_input))) per -02 §4.2 |
| `agent_output_digest` | Task response `{taskId, status, artifact.{name, parts[0].text}}` | SHA-256(JCS(normalize(agent_output))) per -02 §4.2 |
| `action` | `"a2a.boundary_seal"` | constant for this profile |
| `operator` | `"action-state-group"` | the callee operator identity |
| `developer` | `"a2a-sdk==1.1.1@86c6b0d"` | SDK version (pinned) |
| `runtime` | `"draft-mih-scitt-agent-action-capsule-02"` | spec version |
| `verdict` | `"executed"` | terminal state for completed task |
| `effect.type` | `"a2a.task_completed"` | effect descriptor |
| `effect.status` | `"confirmed"` | task ran and completed |
| `effect.task_id` | `params.message.taskId` | links capsule back to task |

**NOT committed directly:** raw message text, artifact content, session tokens. Only digests leave the process.

---

## 2. Digest computation

Per `draft-mih-scitt-agent-action-capsule-02` §4.2:

```
agent_input_digest  = SHA-256(JCS(normalize(agent_input_dict)))
agent_output_digest = SHA-256(JCS(normalize(agent_output_dict)))
capsule_id          = SHA-256(JCS(capsule_body \ {capsule_id, chain}))
```

`normalize()` drops `null` values before JCS serialization.

### agent_input dict structure

```json
{
  "a2a_request": {
    "method": "SendMessage",
    "task_id": "<params.message.taskId>",
    "message_id": "<params.message.messageId>",
    "role": "<params.message.role>",
    "text": "<params.message.parts[0].text>"
  }
}
```

### agent_output dict structure

```json
{
  "a2a_response": {
    "task_id": "<task_id>",
    "status": "completed",
    "artifact": {
      "name": "<artifact name>",
      "parts": [{"text": "<artifact text>"}]
    }
  }
}
```

---

## 3. A2A response extension

The callee includes the following capsule extension in its A2A Task response:

```json
{
  "uri": "https://agentactioncapsule.org/a2a-extension/v1",
  "capsule_id": "<64-char hex SHA-256 capsule_id>",
  "anchor": "https://anchor.agentactioncapsule.org",
  "verify_url": "https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>"
}
```

---

## 4. DENY gate behavior

### `capsule.resolve`

DENY when:
- The `capsule_id` in the extension is not registered on the anchor.
- The anchor returns 404 for `GET /v1/inclusion/<capsule_id>` or `POST /v1/digest`.
- The extension field is absent or malformed.

### `capsule.digest`

DENY when:
- The capsule fetched by `capsule_id` does not contain an `agent_input_digest` matching `SHA-256(JCS(normalize(agent_input)))`.
- `capsule.resolve` failed (no capsule to check digest against).

Both gates DENY independently. A positive result requires both to PASS.

---

## 5. Positive case coordinates (this machine's run)

| Field | Value |
|---|---|
| `task_id` | `task-boundary-seal-001` |
| `capsule_id` | `553b0a2352a25be70d5400434525172e104021f27b8879b1348dc4a53821f046` |
| `input_digest` | `a47245f63ea431a974e3192913d24ba6ea88ab8a830da7d0cb88c38a5a0e2fd5` |
| `output_digest` | `141e722d94e3f3410ae59eb282ff99b75ac0da68b714b221810361adfde5d05d` |
| `anchor_entry_hash` | `41ff4118ffdbf4d14ab00bfe2a8ebe4179bb4b23c2176e3ac44c7106f42babbb` |
| `leaf_index` | `167` |
| `tree_size` | `168` |
| `log_id` | `did:web:anchor.agentactioncapsule.org` |
| `capsule.digest` gate | PASS |
| `capsule.resolve` gate | PASS |

---

## 6. SDK version and shims

**a2a-sdk installed:** 1.1.1 (PyPI), matching commit `86c6b0d` (a2aproject/a2a-python).

**Shims required:** none. The sealing script (`seal_boundary_tuple.py`) extracts task fields from the JSON request directly — no SDK round-trip through protobuf is needed to produce the static sealing input. Anton's two documented shims address server-side message routing; they are orthogonal to a producer-only tuple and are not applicable here. Any discrepancy between his shim set and ours is by design: his shims are for an HTTP server context; ours are for static tuple production.

---

## 7. Verifying this tuple

```bash
# Confirm capsule_id is in the anchor log
curl https://anchor.agentactioncapsule.org/v1/inclusion/553b0a2352a25be70d5400434525172e104021f27b8879b1348dc4a53821f046

# Re-run sealing to confirm determinism (input_digest is stable for the same inputs)
pip install "capsule-emit" "a2a-sdk==1.1.1"
python examples/a2a-ap2/boundary-seal/seal_boundary_tuple.py
# Expect: input_digest = a47245f6...
```
