# RFC9162_SHA256 Receipt Notes

This directory is reserved for live inclusion receipts from
`anchor.agentactioncapsule.org`. The four capsule IDs below are
content-addressed (SHA-256 over the canonical capsule JSON), so anyone
who re-runs `generate.py` from the same input will reproduce the
identical IDs.

## Capsule IDs

| # | Action            | Verdict   | capsule_id (full 64-hex)                                           |
|---|-------------------|-----------|--------------------------------------------------------------------|
| 1 | approve_purchase  | executed  | `705955419ca6f944a75db77ae2a59844fdd99d355866c6c1dbc4ebe655c024c7` |
| 2 | transfer_funds    | blocked   | `cd0692b3349fadfeabe618008301b625059cc819eeb5ca1fb660699be9b6504e` |
| 3 | generate_report   | executed  | `ac0d53a6fef41879e31faf20ae7f73b9d1facf07640c3c1ffc5ae4d8ab26d301` |
| 4 | confirm_purchase  | confirmed | `94c877c7ff0240cf7dafe2067f7016e5412d59b05f9eefa4baf90fc792f16142` |

## Anchor endpoint

Submit any capsule payload to receive an RFC9162_SHA256 COSE receipt:

```http
POST https://anchor.agentactioncapsule.org/anchor
Content-Type: application/json

{
  "payload": "<base64url-encoded capsule JSON>"
}
```

### Example (Python)

```python
import base64, json, httpx

capsule = json.loads(open("sample_ledger.jsonl").readline())
payload_b64 = base64.urlsafe_b64encode(
    json.dumps(capsule, separators=(",", ":")).encode()
).rstrip(b"=").decode()

resp = httpx.post(
    "https://anchor.agentactioncapsule.org/anchor",
    json={"payload": payload_b64},
)
data = resp.json()
print(data.keys())   # dict_keys(['receipt', 'inclusion_proof', ...])
```

### Response structure

```json
{
  "receipt":          "<base64url COSE_Sign1 receipt (RFC 9943 §3)>",
  "inclusion_proof":  {
      "vds":          "RFC9162_SHA256",
      "proof":        ["<base64url node>", "..."],
      "tree_size":    12345,
      "leaf_index":   67
  }
}
```

The `receipt` field is a COSE_Sign1 envelope with:
- Protected header `33` (verifier) pointing to the TS signing key
- Payload: the submitted capsule bytes (detached or inline)
- Unprotected header containing the RFC9162_SHA256 inclusion proof

## Verifying with pyscitt

[pyscitt](https://github.com/microsoft/pyscitt) is the Microsoft/CCF SCITT
client library authored by Amaury Chamayou (co-author of RFC 9943).

```bash
pip install pyscitt scitt-cose
```

```python
from pyscitt.verify import verify_receipt
from pyscitt.client import MemberAuthClient  # or ServiceParameters

# 1. Fetch the TS service parameters (signing cert, VDS).
#    anchor.agentactioncapsule.org exposes them at /.well-known/transparency-configuration

# 2. Call verify_receipt with the COSE receipt bytes.
receipt_bytes = base64.urlsafe_b64decode(data["receipt"] + "==")
verify_receipt(capsule_cose_bytes, receipt_bytes, service_params)
```

The verifier checks:
1. The COSE_Sign1 signature over the receipt (TS key)
2. The RFC9162_SHA256 Merkle inclusion proof (leaf hash, path, root)
3. The signed tree head (STH) binding the root to the log

## Two-TS interop goal

The same capsule should verify clean under two independent Transparency Services:

| TS | VDS | Implementation |
|----|-----|----------------|
| `anchor.agentactioncapsule.org` | RFC9162_SHA256 | capsule-anchor (this project) |
| Any CCF-backed TS | CCF VDS | pyscitt / microsoft/CCF |

Both are conforming SCITT Transparency Services per RFC 9943
(draft-ietf-scitt-architecture). The capsule payload is VDS-agnostic — the
same JSON bytes anchor to any conforming TS without modification.

## scitt-cose standalone verifier

`scitt-cose` (repo: `action-state-group/scitt-cose`) is a vendor-neutral
RFC9162_SHA256 receipt verifier:

```bash
pip install scitt-cose
scitt-cose verify --receipt receipt.cbor --payload capsule.json \
    --service-params https://anchor.agentactioncapsule.org/.well-known/transparency-configuration
```

## Specification reference

- Individual I-D: <https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/>
- SCITT architecture: RFC 9943 / draft-ietf-scitt-architecture
- Receipts: draft-ietf-scitt-scrapi
- RFC9162_SHA256 VDS: RFC 9162 (Certificate Transparency v2 hash algorithm)
