# Format-4 cross-language interoperability vectors

This pack freezes one implementation-neutral input flow and the exact outputs
needed to compare independent producers:

- authored `seal()` with input/output commitments and model/runtime metadata;
- opaque `received()` carry;
- WHO and DID member Capsules;
- a WHO/CAN/DID slot composition that references those existing members;
- detached Ed25519 COSE Producer Envelopes for every Capsule.

`capsule.detached.jcs` is the canonical, signer-independent Capsule including
`capsule_id`. `envelope.cose` is the detached Producer Envelope.
`capsule.stored.json` is the Python ledger representation, which adds the local
`signature` and `key_id` fields. Implementations compare the detached Capsule
and Envelope byte-for-byte; they verify the stored form without treating its
JSON member order as a language-neutral contract.

The shared input deliberately uses a six-digit fractional timestamp ending in
zeros and supplies slot members out of canonical order. Those values catch
timestamp reformatting and implementations that preserve caller slot order
instead of emitting WHO/CAN/DID/AUDIT order.

Regenerate from the repository root:

```bash
python test-vectors/format4-interop/scripts/generate_vectors.py
```

The seed is public test material and must never be used as a production key.
