# Test fixtures

- `evidencebook_parity.json`, `evidencebook_parity_inputs.json`: legacy
  byte-parity data. The inputs are the header strings and
  `compute_attestation` extension members an earlier producer committed; the
  field names in them (`x-mesh-poc-v1`, `tee_attestation`, a
  `mesh-poc/...` action id, a `capsule-producer/...` developer string) are
  historical and are kept byte for byte, because the pinned capsule ids are
  computed over them. This crate does not interpret them.
- `python_produced_noncanonical_envelope.hex`: a COSE_Sign1 producer envelope
  from the Python reference whose protected header is in non-canonical label
  order (3, 4, 1). `cross_language_conformance.rs` checks that it verifies as
  received.
