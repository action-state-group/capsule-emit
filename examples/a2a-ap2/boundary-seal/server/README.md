# AAC Boundary-Seal A2A Server + loopback rehearsal

A runnable A2A JSON-RPC server (a2a-sdk 1.1.1) whose single skill seals each
task's input+output as an AAC capsule, anchors it on the transparency log, and
returns the capsule reference under the AAC extension. Companion to the frozen
producer tuple in the parent directory — this is the *served* agent for a live,
cross-network bilateral close.

## Files
- `serve_boundary_agent.py` — the A2A server. Serves the Agent Card at
  `/.well-known/agent-card.json` (extension advertised) and JSON-RPC at `/a2a`.
  Each task: seal (capsule-emit) → **single** `POST /v1/digest` that anchors +
  resolves → returns `capsule_id`, `entry_hash`, `leaf_index`, `tree_size`,
  `inclusion_proof_url` as artifact metadata under
  `https://agentactioncapsule.org/a2a-extension/v1`.
- `rehearse_loopback.py` — our A2A client against our own endpoint (loopback).
  **Classified as a rehearsal**, not the close. Verifies the returned capsule
  reference OFFLINE via `scitt-cose` (`verify_receipt` + RFC6962 `verify_inclusion`).

## Run
```bash
pip install "capsule-emit" "a2a-sdk==1.1.1" "scitt-cose" uvicorn fastapi
python server/serve_boundary_agent.py --host 127.0.0.1 --port 8080 &
python server/rehearse_loopback.py
```

Expected: task `TASK_STATE_COMPLETED`; `verify_receipt ok=True`;
`verify_inclusion True`; the rehearsal prints its coordinates (record these
**separately** from any close).

## Notes
- **Submit exactly once.** The server seals with `emit(anchor=False)` and anchors
  in the single `POST /v1/digest`. Submitting the same new `capsule_id` twice can
  double-append it on the anchor (one statement at two CT leaves) — see the
  anchor-side atomic-dedup fix. One submission → one leaf.
- **DENY negative** is verifier-side: resolving a fabricated `capsule_id` returns
  DENY (404 on the read-only resolve route / "no cached statement" offline).
- For a public cross-network close, run behind a reachable URL and pass
  `--public-url https://<host>` so the Agent Card advertises the external address.
